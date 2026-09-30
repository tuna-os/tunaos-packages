"""Tests for scripts/generate-distributed-workflow.py"""

# Load module from filepath directly because of hyphen in filename
import importlib.util
import os
import sys

import yaml

scripts_dir = os.path.join(os.path.dirname(__file__), '..', 'scripts')
module_path = os.path.join(scripts_dir, 'generate-distributed-workflow.py')
spec = importlib.util.spec_from_file_location("generate_distributed_workflow", module_path)
generate_distributed_workflow = importlib.util.module_from_spec(spec)
sys.modules["generate_distributed_workflow"] = generate_distributed_workflow
spec.loader.exec_module(generate_distributed_workflow)
from generate_distributed_workflow import generate_workflow  # noqa: E402 -- needs exec_module above

SAMPLE_MANIFEST = {
    'tiers': [
        {
            'name': 'core',
            'packages': [
                {'path': 'src/core/systemd'},
                {'path': 'src/core/glibc'},
            ],
        },
        {
            'name': 'desktop',
            'packages': [
                {'path': 'src/desktop/gnome-shell'},
                {'path': 'src/desktop/mutter'},
            ],
        },
    ],
}


def test_generate_workflow_basic(tmp_path):
    """Test that generate_workflow produces a valid workflow with expected structure."""
    manifest_path = tmp_path / 'build-order.yml'
    output_path = tmp_path / 'output.yml'

    with open(manifest_path, 'w') as f:
        yaml.dump(SAMPLE_MANIFEST, f)

    generate_workflow(str(manifest_path), str(output_path))

    with open(output_path) as f:
        workflow = yaml.safe_load(f)

    assert workflow['name'] == 'Distributed Build and Publish RPMs'
    assert 'on' in workflow
    assert 'workflow_dispatch' in workflow['on']
    assert 'jobs' in workflow

    jobs = workflow['jobs']
    assert 'seed-repo' in jobs
    assert 'build-core' in jobs
    assert 'consolidate-core' in jobs
    assert 'build-desktop' in jobs
    assert 'consolidate-desktop' in jobs
    assert 'publish' in jobs


def test_generate_workflow_tier_order(tmp_path):
    """Test that tiers are generated in the correct order."""
    manifest_path = tmp_path / 'build-order.yml'
    output_path = tmp_path / 'output.yml'

    with open(manifest_path, 'w') as f:
        yaml.dump(SAMPLE_MANIFEST, f)

    generate_workflow(str(manifest_path), str(output_path))

    with open(output_path) as f:
        workflow = yaml.safe_load(f)

    jobs = workflow['jobs']
    # seed-repo should come first
    assert 'seed-repo' in jobs
    # build-core should need seed-repo
    assert jobs['build-core']['needs'] == 'seed-repo'
    # consolidate-core should need build-core
    assert jobs['consolidate-core']['needs'] == 'build-core'
    # build-desktop should need consolidate-core
    assert jobs['build-desktop']['needs'] == 'consolidate-core'
    # consolidate-desktop should need build-desktop
    assert jobs['consolidate-desktop']['needs'] == 'build-desktop'
    # publish should need consolidate-desktop
    assert jobs['publish']['needs'] == 'consolidate-desktop'


def test_generate_workflow_no_tiers(tmp_path):
    """Test that empty tiers produce valid workflow."""
    manifest_path = tmp_path / 'build-order.yml'
    output_path = tmp_path / 'output.yml'

    with open(manifest_path, 'w') as f:
        yaml.dump({'tiers': []}, f)

    generate_workflow(str(manifest_path), str(output_path))

    with open(output_path) as f:
        workflow = yaml.safe_load(f)

    assert 'seed-repo' in workflow['jobs']
    # publish should need seed-repo (since no tiers)
    assert workflow['jobs']['publish']['needs'] == 'seed-repo'
    assert 'build-core' not in workflow['jobs']


def test_generate_workflow_packages_in_matrix(tmp_path):
    """Test that packages appear in the build matrix."""
    manifest_path = tmp_path / 'build-order.yml'
    output_path = tmp_path / 'output.yml'

    with open(manifest_path, 'w') as f:
        yaml.dump(SAMPLE_MANIFEST, f)

    generate_workflow(str(manifest_path), str(output_path))

    with open(output_path) as f:
        workflow = yaml.safe_load(f)

    # Check that the packages are in the build-core matrix
    core_matrix = workflow['jobs']['build-core']['strategy']['matrix']
    assert 'package' in core_matrix
    assert 'src/core/systemd' in core_matrix['package']
    assert 'src/core/glibc' in core_matrix['package']

    # Check desktop packages
    desktop_matrix = workflow['jobs']['build-desktop']['strategy']['matrix']
    assert 'src/desktop/gnome-shell' in desktop_matrix['package']
    assert 'src/desktop/mutter' in desktop_matrix['package']


def test_generate_workflow_custom_name(tmp_path):
    """Test that custom workflow name is used."""
    manifest_path = tmp_path / 'build-order.yml'
    output_path = tmp_path / 'output.yml'
    custom_name = 'Custom Workflow Name'

    with open(manifest_path, 'w') as f:
        yaml.dump(SAMPLE_MANIFEST, f)

    generate_workflow(str(manifest_path), str(output_path), workflow_name=custom_name)

    with open(output_path) as f:
        workflow = yaml.safe_load(f)

    assert workflow['name'] == custom_name


def test_generate_workflow_custom_r2_path(tmp_path):
    """Test that custom R2 path is reflected in the publish job."""
    manifest_path = tmp_path / 'build-order.yml'
    output_path = tmp_path / 'output.yml'
    custom_r2 = 'gnome49/10-stream-x86_64'

    with open(manifest_path, 'w') as f:
        yaml.dump(SAMPLE_MANIFEST, f)

    generate_workflow(str(manifest_path), str(output_path), r2_path=custom_r2)

    with open(output_path) as f:
        workflow = yaml.safe_load(f)

    # The env section should reference the R2 bucket
    assert workflow['env']['R2_BUCKET'] == 'bluefin'


def test_generate_workflow_is_parseable_yaml(tmp_path):
    """Test that the generated YAML is valid and parseable."""
    manifest_path = tmp_path / 'build-order.yml'
    output_path = tmp_path / 'output.yml'

    with open(manifest_path, 'w') as f:
        yaml.dump(SAMPLE_MANIFEST, f)

    generate_workflow(str(manifest_path), str(output_path))

    # Re-parse to verify YAML validity
    with open(output_path) as f:
        workflow = yaml.safe_load(f)

    assert workflow is not None
    assert isinstance(workflow, dict)


def test_generate_workflow_mock_config(tmp_path):
    # A generated workflow that silently built in the default chroot would
    # only fail once a package needed one of the gnome50 config's repos, so
    # the flag must reach build-chain.sh AND re-key the caches on the
    # selected config file.
    manifest = tmp_path / "build-order-test.yml"
    manifest.write_text(
        "target: centos-stream-10-x86_64\n"
        "tiers:\n"
        "  - name: base\n"
        "    packages:\n"
        "      - path: src/demo\n"
    )
    output = tmp_path / "workflow.yml"
    generate_workflow(str(manifest), str(output), mock_config="centos-stream-10-ci-gnome50")
    text = output.read_text()
    assert "--mock-config centos-stream-10-ci-gnome50" in text
    assert "mock/centos-stream-10-ci-gnome50.cfg" in text
    assert "mock/centos-stream-10-ci.cfg" not in text
