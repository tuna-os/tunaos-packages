"""Alma native identities and namespaces cannot collapse into CentOS supply.

Falsification: x86_64 is a package architecture, not proof of a v2 CPU floor;
ARM must select its own immutable native image rather than an x86 child.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from target_platform import build_context


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


planner = load_module('alma_factory_planner', 'scripts/plan-package-factory.py')
publisher = load_module('alma_rpm_publisher', 'scripts/plan-rpm-publish.py')
FACTORY = yaml.safe_load((ROOT / 'manifests/package-factory.yaml').read_text())
PINS = {
    ('alma10', 'x86_64'): '51d5589de16b26f7145a20e2eba45aa2030fa12a2f9782b04fe5a4e8e6dc7912',
    ('alma10', 'aarch64'): 'f65410c601aa3b514801aecf8d10eeefaa5b6a56248279b96e4085f1861b7d64',
    ('alma10-kitten', 'x86_64'): 'a85da6343aca56df44ae45493a78bda56d4055d6e2d4c950ba81c7e25b25491a',
    ('alma10-kitten', 'aarch64'): 'ea0cc0a1592d6ee73727e9efe5628094d0e4e12a762e26bce966592ad5d1ef6c',
}
IDENTITIES = {'x86_64': ('linux/amd64/v2', 'x86-64-v2'), 'aarch64': ('linux/arm64', 'armv8-a')}


@pytest.mark.parametrize('target,arch', PINS)
def test_actual_planner_and_publisher_keep_exact_native_identity(target, arch):
    image = 'quay.io/almalinuxorg/almalinux@sha256:' + PINS[target, arch]
    platform, baseline = IDENTITIES[arch]
    cells = planner.tideforge_cells(ROOT)
    cell = next(c for c in cells if c['package'] == 'cosmic-session'
                and c['target'] == target and c['architecture'] == arch)
    assert cell['image'] == cell['verify_image'] == image
    assert cell['platform'] == platform
    assert cell['cpu_baseline'] == baseline
    result = publisher.plan(target, ['cosmic-session'], [arch])
    build, publish = result['build']['include'][0], result['publish']['include'][0]
    for row in (build, publish):
        assert row['image'] == image
        assert row['platform'] == platform
        assert row['cpu_baseline'] == baseline
    assert publish['src'] == f'rpm/{target}/{arch}'
    assert publish['served'] == f'https://repo.tunaos.org/rpm/{target}/{arch}/'
    assert publish['mirror'] == ''
    assert publish['min_rpms'] == 0
    assert FACTORY['targets'][target]['published_index_pending'] == ['x86_64', 'aarch64']
    assert not FACTORY['targets'][target].get('published_index')


def test_alma_namespaces_do_not_overwrite_existing_centos_routes():
    cs = publisher.plan('el10', ['cosmic-session'], ['x86_64', 'aarch64'])
    rows = cs['publish']['include']
    assert rows[0]['src'] == 'repo/10-stream-x86_64'
    assert rows[0]['mirror'] == 'repo/10-x86_64'
    assert rows[1]['src'] == 'rpm/el10/aarch64'
    destinations = {row['src'] for row in rows}
    for target in ('alma10', 'alma10-kitten'):
        for row in publisher.plan(target, ['cosmic-session'], None)['publish']['include']:
            assert row['src'] not in destinations
            destinations.add(row['src'])
    assert len(destinations) == 6


def test_initial_alma_candidate_cannot_restore_unverified_partial_rpms():
    """Falsification: CI restored ten prior RPMs before the empty signed bootstrap."""
    workflow = yaml.safe_load((ROOT / '.github/workflows/package-factory-cell.yml').read_text())
    restore = next(step for step in workflow['jobs']['build']['steps']
                   if 'restore-partial-chain-output.py' in step.get('run', ''))
    assert "matrix.engine == 'build-chain'" in restore['if']
    assert "steps.verdict.outputs.hit != 'true'" in restore['if']
    assert "!startsWith(matrix.mock_config, 'alma10-')" in restore['if']
    assert 'restore-partial-chain-output.py' in restore['run']


def test_all_twelve_native_family_cells_use_isolated_configs_and_pins():
    cells = [cell for cell in planner.native_cells(ROOT)
             if cell['target'] in ('alma10', 'alma10-kitten')]
    assert len(cells) == 12
    expected_ids = {f'{family}-{target}-{arch}' for family in ('gnome50', 'gnome51', 'xfce')
                    for target, arch in PINS}
    assert {cell['id'] for cell in cells} == expected_ids
    prefixes = set()
    for cell in cells:
        target, arch, family = cell['target'], cell['architecture'], cell['family']
        assert cell['platform'] == IDENTITIES[arch][0]
        assert cell['cpu_baseline'] == IDENTITIES[arch][1]
        assert cell['verify_image'] == 'quay.io/almalinuxorg/almalinux@sha256:' + PINS[target, arch]
        suffix = '-aarch64' if arch == 'aarch64' else ''
        assert cell['mock_config'] == f'{target}-ci-{family}{suffix}'
        assert (ROOT / 'mock' / (cell['mock_config'] + '.cfg')).is_file()
        assert cell['r2_path'] == f'{family}/{target}-{arch}'
        prefixes.add(cell['r2_path'])
    assert len(prefixes) == 12


@pytest.mark.parametrize('target', ['alma10', 'alma10-kitten'])
def test_cosmic_queue_keeps_all_twenty_five_roots_and_gates(target):
    queue = yaml.safe_load((ROOT / 'manifests/target-queues/cosmic.yaml').read_text())['queues']
    assert len(queue[target]['roots']) == 25
    assert queue[target]['roots'] == queue['el10']['roots']
    assert queue[target]['gates'] == ['mock-build', 'rpm-md-stage-install', 'greetd-login', 'cosmic-session-smoke']
    assert queue[target]['format'] == 'rpm'


@pytest.mark.parametrize('arch', ['x86_64', 'aarch64'])
@pytest.mark.parametrize('mutation,message', [
    ('missing-image', 'missing native probe image'),
    ('tag-image', 'digest-pinned'),
    ('short-digest', 'digest-pinned'),
    ('bad-platform', 'platform/baseline'),
    ('bad-baseline', 'platform/baseline'),
    ('missing-platform', 'platform/baseline'),
    ('missing-baseline', 'platform/baseline'),
])
def test_invalid_architecture_specific_inputs_fail_explicitly(arch, mutation, message, tmp_path):
    target = copy.deepcopy(FACTORY['targets']['alma10'])
    if mutation == 'missing-image':
        del target['probe_images'][arch]
    elif mutation == 'tag-image':
        target['probe_images'][arch] = 'quay.io/almalinuxorg/almalinux:10'
    elif mutation == 'short-digest':
        target['probe_images'][arch] = 'quay.io/almalinuxorg/almalinux@sha256:abc'
    elif mutation == 'bad-platform':
        target['platforms'][arch] = 'linux/amd64' if arch == 'x86_64' else 'linux/amd64/v2'
    elif mutation == 'bad-baseline':
        target['cpu_baselines'][arch] = 'x86-64-v3'
    elif mutation == 'missing-platform':
        del target['platforms'][arch]
    else:
        del target['cpu_baselines'][arch]
    with pytest.raises(ValueError, match=message):
        build_context(target, arch)
    factory = copy.deepcopy(FACTORY)
    factory['targets']['alma10'] = target
    (tmp_path / 'manifests').mkdir()
    (tmp_path / 'manifests/package-factory.yaml').write_text(yaml.safe_dump(factory))
    recipe_dir = tmp_path / 'packages/cosmic-session'
    recipe_dir.mkdir(parents=True)
    shutil.copyfile(ROOT / 'packages/cosmic-session/package.yaml', recipe_dir / 'package.yaml')
    with pytest.raises(ValueError, match=message):
        planner.tideforge_cells(tmp_path)


@pytest.mark.parametrize('arch', ['x86_64', 'aarch64'])
def test_actual_planner_blocks_missing_architecture_native_image(tmp_path, arch):
    factory = copy.deepcopy(FACTORY)
    del factory['targets']['alma10']['probe_images'][arch]
    (tmp_path / 'manifests').mkdir()
    (tmp_path / 'manifests/package-factory.yaml').write_text(yaml.safe_dump(factory))
    recipe_dir = tmp_path / 'packages/cosmic-session'
    recipe_dir.mkdir(parents=True)
    shutil.copyfile(ROOT / 'packages/cosmic-session/package.yaml', recipe_dir / 'package.yaml')
    with pytest.raises(ValueError, match='missing native probe image'):
        planner.tideforge_cells(tmp_path)


@pytest.mark.parametrize('mutation', ['missing-verify-image', 'tag-verify-image', 'wrong-platform', 'wrong-baseline', 'wrong-arch-pin', 'centos-config', 'centos-namespace'])
def test_native_family_planner_rejects_inconsistent_alma_identity(tmp_path, mutation):
    registry = yaml.safe_load((ROOT / 'manifests/package-builds.yaml').read_text())
    cell = copy.deepcopy(next(row for row in registry['native_builds']
                              if row['id'] == 'gnome50-alma10-x86_64'))
    if mutation == 'missing-verify-image':
        del cell['verify_image']
    elif mutation == 'tag-verify-image':
        cell['verify_image'] = 'quay.io/almalinuxorg/almalinux:10'
    elif mutation == 'wrong-platform':
        cell['platform'] = 'linux/amd64/v3'
    elif mutation == 'wrong-baseline':
        cell['cpu_baseline'] = 'x86-64-v3'
    elif mutation == 'wrong-arch-pin':
        cell['verify_image'] = 'quay.io/almalinuxorg/almalinux@sha256:' + PINS['alma10', 'aarch64']
    elif mutation == 'centos-config':
        cell['mock_config'] = 'centos-stream-10-ci-gnome50'
    else:
        cell['r2_path'] = 'gnome50/10-stream-x86_64'
    (tmp_path / 'manifests').mkdir()
    (tmp_path / 'manifests/package-builds.yaml').write_text(yaml.safe_dump({'native_builds': [cell]}))
    (tmp_path / 'manifests/package-factory.yaml').write_text(yaml.safe_dump(FACTORY))
    destination = tmp_path / cell['manifest']
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / cell['manifest'], destination)
    with pytest.raises(ValueError, match='(native|Alma|platform|baseline|image)'):
        planner.native_cells(tmp_path)


@pytest.mark.parametrize('target,arch', PINS)
def test_dependency_probe_dry_run_uses_native_child_and_signed_native_urls(target, arch):
    result = subprocess.run([
        sys.executable, str(ROOT / 'scripts/probe-target-dependencies.py'),
        str(ROOT / 'packages/cosmic-session/package.yaml'), '--target', target,
        '--architecture', arch, '--dry-run', '--json',
    ], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)[target]
    assert report['status'] == 'not-run'
    assert report['image'] == 'quay.io/almalinuxorg/almalinux@sha256:' + PINS[target, arch]
    assert 'cargo' in report['dependencies']
    probe = load_module('alma_dependency_probe', 'scripts/probe-target-dependencies.py')
    command = probe.podman_command(report['image'], target, ['cargo'], ['crb', 'epel'], arch)
    assert command[3] == report['image']
    script = command[6]
    repo_arch = 'x86_64_v2' if arch == 'x86_64' else 'aarch64'
    host = ('https://repo.almalinux.org/almalinux/10' if target == 'alma10'
            else 'https://kitten.repo.almalinux.org/10-kitten')
    assert f'{host}/BaseOS/{repo_arch}/os/' in script
    branch = '10z' if target == 'alma10' else '10'
    if arch == 'x86_64':
        assert f'https://epel.repo.almalinux.org/{branch}/x86_64_v2/' in script
        assert 'fedoraproject.org' not in script
    else:
        assert f'https://dl.fedoraproject.org/pub/epel/{branch}/Everything/aarch64/' in script
        assert 'x86_64_v2/' not in script
    for forbidden in ('centos', 'kojihub', 'kojipkgs', 'gpgcheck = 0', 'nogpgcheck', 'local-build', 'tunaos-rpm'):
        assert forbidden not in script.lower()
    assert '--setopt=gpgcheck=1' in script


@pytest.mark.parametrize('arch', ['x86_64', 'aarch64'])
@pytest.mark.parametrize('only_alma', [False, True])
def test_portable_candidate_plan_does_not_alias_unproved_payload_into_alma(tmp_path, arch, only_alma):
    recipe_dir = tmp_path / 'packages/baseline-canary'
    recipe_dir.mkdir(parents=True)
    (recipe_dir / 'package.yaml').write_text('''schema: 1
name: baseline-canary
version: '1.0'
release: 1
summary: baseline canary
description: static Go still requires an Alma CPU proof
license: MIT
source:
  url: https://example.invalid/baseline-canary.tar.gz
  sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
build_system: go
build:
  environment: {CGO_ENABLED: '0'}
files: {common: [usr/bin/baseline-canary]}
targets: [el10, ubuntu, alma10, alma10-kitten]
''')
    targets = ['alma10', 'alma10-kitten'] if only_alma else ['el10', 'ubuntu', 'alma10', 'alma10-kitten']
    result = subprocess.run([
        sys.executable, str(ROOT / 'scripts/tideforge-intermediate.py'), 'candidates',
        '--root', str(tmp_path / 'packages'), '--architecture', arch, '--targets', *targets,
    ], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    if only_alma:
        assert report['payload_count'] == report['carrier_count'] == 0
        assert report['carriers']['include'] == []
    else:
        assert report['payload_count'] == 1
        assert report['carrier_count'] == 2
        assert {row['target'] for row in report['carriers']['include']} == {'el10', 'ubuntu'}
        assert all(row['package'] == 'baseline-canary' for row in report['carriers']['include'])
