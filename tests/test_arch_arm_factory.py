"""CI-only native Arch architecture/signature policy regressions."""
from __future__ import annotations

import copy
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from target_platform import build_context

SPEC = importlib.util.spec_from_file_location('arch_arm_planner', ROOT / 'scripts/plan-package-factory.py')
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)
FACTORY = yaml.safe_load((ROOT / 'manifests/package-factory.yaml').read_text())


def test_arch_uses_distinct_exact_native_children():
    arch = FACTORY['targets']['arch']
    intel = build_context(arch, 'x86_64')
    arm = build_context(arch, 'aarch64')
    assert intel['platform'] == 'linux/amd64'
    assert intel['cpu_baseline'] == 'x86-64'
    assert arm['platform'] == 'linux/arm64'
    assert arm['cpu_baseline'] == 'armv8-a'
    assert intel['image'] != arm['image']
    assert '@sha256:' in intel['image'] and '@sha256:' in arm['image']
    assert arm['image'].startswith('ghcr.io/tuna-os/archlinuxarm@sha256:')


@pytest.mark.parametrize('architecture', ['x86_64', 'aarch64'])
@pytest.mark.parametrize('mutation', ['tag', 'missing', 'wrong-platform', 'wrong-baseline'])
def test_missing_or_mismatched_native_input_blocks(architecture, mutation):
    target = copy.deepcopy(FACTORY['targets']['arch'])
    if mutation == 'tag':
        target['probe_images'][architecture] = 'docker.io/library/archlinux:latest'
    elif mutation == 'missing':
        del target['probe_images'][architecture]
    elif mutation == 'wrong-platform':
        target['platforms'][architecture] = 'linux/amd64/v2'
    else:
        target['cpu_baselines'][architecture] = 'x86-64-v3'
    with pytest.raises(ValueError):
        build_context(target, architecture)


def test_every_arch_recipe_emits_both_native_legs():
    cells = planner.tideforge_cells(ROOT)
    by_recipe = {}
    for cell in cells:
        if cell['target'] != 'arch':
            continue
        by_recipe.setdefault(cell['recipe'], set()).add(cell['architecture'])
        assert cell['image'] == build_context(FACTORY['targets']['arch'], cell['architecture'])['image']
        assert cell['verify_image'] == cell['image']
        if cell['architecture'] == 'aarch64':
            assert cell['runner'] == 'ubuntu-24.04-arm'
    assert by_recipe
    assert all(architectures == {'x86_64', 'aarch64'} for architectures in by_recipe.values())


def helper(tmp_path, architecture='aarch64', global_policy='Required DatabaseOptional',
           override='', repositories='core\nextra\nalarm\naur', expected=None, operation='repositories'):
    commands = tmp_path / 'bin'
    commands.mkdir()
    uname = commands / 'uname'
    uname.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_ARCH"\n')
    pacman = commands / 'pacman-conf'
    pacman.write_text('''#!/bin/bash
case "$1" in
 Architecture) printf '%s\\n' "$FAKE_ARCH" ;;
 --repo-list) printf '%s\\n' "$FAKE_REPOS" ;;
 SigLevel) printf '%s\\n' "$FAKE_GLOBAL" ;;
 --repo) printf '%s\\n' "$FAKE_OVERRIDE" ;;
 *) exit 9 ;;
esac
''')
    key = commands / 'pacman-key'
    key.write_text('#!/bin/sh\necho forbidden-side-effect >&2\nexit 19\n')
    for script in (uname, pacman, key):
        script.chmod(0o755)
    env = dict(os.environ, PATH=f'{commands}:{os.environ["PATH"]}', FAKE_ARCH=architecture,
               FAKE_REPOS=repositories, FAKE_GLOBAL=global_policy, FAKE_OVERRIDE=override)
    return subprocess.run(['bash', str(ROOT / 'scripts/arch-native-policy.sh'), operation,
                           architecture if expected is None else expected], env=env,
                          capture_output=True, text=True, timeout=10)


def test_arm_preserves_all_native_signed_repositories(tmp_path):
    result = helper(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['core', 'extra', 'alarm', 'aur']


def test_intel_preserves_official_native_repositories(tmp_path):
    result = helper(tmp_path, architecture='x86_64', repositories='core\nextra\nmultilib')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('global_policy,override', [
    ('Required DatabaseOptional', 'PackageOptional'),
    ('Required DatabaseOptional', 'TrustAll'),
    ('Optional', ''), ('Never', ''), ('Required UnknownPolicy', ''),
])
def test_native_signature_weakening_rejected_before_key_or_transaction(tmp_path, global_policy, override):
    result = helper(tmp_path, global_policy=global_policy, override=override, operation='configure')
    assert result.returncode != 0
    assert 'forbidden-side-effect' not in result.stderr


def test_database_optional_does_not_weaken_package_signatures(tmp_path):
    result = helper(tmp_path, global_policy='PackageRequired PackageTrustedOnly DatabaseOptional')
    assert result.returncode == 0, result.stderr


def test_authored_repo_can_strengthen_inherited_package_policy(tmp_path):
    result = helper(tmp_path, global_policy='Optional', override='PackageRequired PackageTrustedOnly')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('architecture,repositories,expected', [
    ('aarch64', 'core\nextra\nmultilib', 'aarch64'),
    ('x86_64', 'core\nextra\nalarm', 'x86_64'),
    ('aarch64', 'core\nextra', 'x86_64'),
    ('riscv64', 'core\nextra', 'riscv64'),
])
def test_foreign_architecture_or_repository_blocks(tmp_path, architecture, repositories, expected):
    result = helper(tmp_path, architecture=architecture, repositories=repositories, expected=expected)
    assert result.returncode != 0


def test_native_mirrors_and_signature_policy_shared_by_all_consumers():
    policy = (ROOT / 'scripts/arch-native-policy.sh').read_text()
    assert 'https://fl.us.mirror.archlinuxarm.org/$arch/$repo' in policy
    assert 'https://geo.mirror.pkgbuild.com/$repo/os/$arch' in policy
    for name in ['run-package-factory-cell.sh', 'arch-clean-install.sh', 'arch-verify-published.sh']:
        source = (ROOT / 'scripts' / name).read_text()
        assert 'arch-native-policy.sh' in source
        assert 'geo.mirror.pkgbuild.com' not in source
    verifier = (ROOT / 'scripts/verify-package-factory-cell.sh').read_text()
    assert '--env TUNAOS_ARCHITECTURE="${ARCHITECTURE:?}"' in verifier


def test_native_policy_changes_invalidate_arch_selection():
    assert planner.FORMAT_INPUTS['scripts/arch-native-policy.sh'] == {'pkg.tar.zst'}


@pytest.mark.parametrize('repositories', ['aur', 'core', 'extra', 'core\ncore\nextra'])
def test_missing_or_duplicate_required_native_repositories_block(tmp_path, repositories):
    result = helper(tmp_path, repositories=repositories)
    assert result.returncode != 0


def test_publisher_uses_exact_per_architecture_image_runner_and_identity():
    workflow = yaml.safe_load((ROOT / '.github/workflows/publish-tideforge-arch.yml').read_text())
    publish = workflow['jobs']['publish']
    assert publish['runs-on'] == '${{ matrix.runner }}'
    text = (ROOT / '.github/workflows/publish-tideforge-arch.yml').read_text()
    assert 'needs.plan.outputs.image' not in text
    assert text.count('matrix.image') >= 3
    assert 'TUNAOS_ARCHITECTURE' in text


def test_publisher_rows_cannot_reuse_x86_image_for_arm():
    specification = importlib.util.spec_from_file_location('native_arch_publish', ROOT / 'scripts/plan-arch-publish.py')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    result = module.plan(['wayland-protocols'], ['x86_64', 'aarch64'])
    assert result['image'] == ''
    for key in ('build', 'publish'):
        rows = result[key]['include']
        assert {row['arch'] for row in rows} == {'x86_64', 'aarch64'}
        for row in rows:
            assert row['image'] == build_context(FACTORY['targets']['arch'], row['arch'])['image']
            if row['arch'] == 'aarch64':
                assert row['runner'] == 'ubuntu-24.04-arm'
