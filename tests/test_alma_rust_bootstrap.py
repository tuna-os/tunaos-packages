"""Source contract regressions; native compiler builds run only in CI."""
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import tideforge

CONSUMERS = ['cosmic-bg', 'cosmic-panel', 'cosmic-app-library', 'cosmic-files',
    'cosmic-comp', 'cosmic-greeter', 'cosmic-initial-setup', 'cosmic-notifications',
    'cosmic-launcher', 'cosmic-session', 'cosmic-osd', 'cosmic-settings',
    'cosmic-term', 'cosmic-workspaces', 'cosmic-applets', 'cosmic-osk', 'cosmic-settings-daemon']


def load(name):
    return yaml.safe_load((ROOT / 'packages' / name / 'package.yaml').read_text())


@pytest.mark.parametrize('target', ['alma10', 'alma10-kitten'])
def test_source_provider_renders_without_compiler_policy_reset(target):
    recipe = load('tunaos-rust193')
    tideforge.validate(recipe, target)
    output = '\n'.join(tideforge.render(recipe, target).values())
    assert '/usr/bin/rustc' in output and '/usr/bin/cargo' in output
    assert 'tunaos_alma_compiler_policy' in output
    assert 'download-rustc = false' in output
    assert 'download-ci-llvm = false' in output
    assert 'download-ci-gcc = false' in output
    assert 'vendor = true' in output and 'locked-deps = true' in output
    assert 'DESTDIR=%{buildroot}' in output
    assert 'BuildRequires: rust >= 1.92' in output
    assert 'BuildRequires: cargo >= 1.92' in output
    assert 'Provides:       rust' not in output
    assert 'Provides:       cargo' not in output


def test_source_archive_immutable_and_isolated_full_toolchain():
    recipe = load('tunaos-rust193')
    assert recipe['source']['url'] == 'https://static.rust-lang.org/dist/rustc-1.93.0-src.tar.xz'
    assert recipe['source']['sha256'] == 'e30d898272c587a22f77679f03c5e8192b5645c7c9ccc3407ad1106761507cea'
    assert recipe['files']['common'] == ['opt/tunaos/rust-1.93']
    commands = '\n'.join(recipe['build']['commands'])
    assert 'compiler/rustc library/std src/tools/cargo' in commands
    assert recipe['build']['environment']['CARGO_NET_OFFLINE'] == 'true'
    assert 'signed native Rust 1.92 bootstrap required' in commands
    assert 'native bootstrap host mismatch' in commands
    assert 'os.environ[\'RUSTFLAGS\']' in commands
    assert 'os.environ[\'CFLAGS\']' in commands
    assert 'os.environ[\'CXXFLAGS\']' in commands


@pytest.mark.parametrize('target', ['alma10', 'alma10-kitten'])
@pytest.mark.parametrize('name', CONSUMERS)
def test_cosmic_exact_provider_and_path_before_cargo(target, name):
    recipe = load(name)
    assert 'tunaos-rust193 >= 1.93.0' in tideforge.target_dependencies(recipe, target)
    output = '\n'.join(tideforge.render(recipe, target).values())
    # cosmic-osd invokes Cargo through its pinned upstream just build command.
    # Preserve the actual authored entry point as well as policy ordering.
    command = recipe['build']['commands'][0]
    assert output.index('export PATH=/opt/tunaos/rust-1.93/bin:/usr/bin:/bin') < output.index(command)
    assert output.index('export PATH=/opt/tunaos/rust-1.93/bin:/usr/bin:/bin') < output.index('tunaos_alma_compiler_policy %{_target_cpu}')
    assert 'BuildRequires: tunaos-rust193 >= 1.93.0' in output


@pytest.mark.parametrize('target', ['el10', 'ubuntu', 'debian'])
@pytest.mark.parametrize('name', CONSUMERS)
def test_other_targets_do_not_require_isolated_alma_provider(target, name):
    assert not any('tunaos-rust193' in item for item in tideforge.target_dependencies(load(name), target))
    assert 'opt/tunaos/rust-1.93/bin' not in '\n'.join(tideforge.render(load(name), target).values())


@pytest.mark.parametrize('mapping', [[], {'missing-target': {}}, {'alma10': []},
    {'alma10': {'lowercase': 'bad'}}, {'alma10': {'PATH': 1}}])
def test_invalid_target_environment_mapping_rejected(mapping):
    recipe = load('cosmic-bg')
    recipe['build']['environment_by_target'] = mapping
    with pytest.raises(SystemExit):
        tideforge.validate(recipe, 'alma10')


def test_target_environment_cannot_reset_native_cpu_policy():
    recipe = load('cosmic-bg')
    recipe['build']['environment_by_target']['alma10']['RUSTFLAGS'] = '-C target-cpu=native'
    with pytest.raises(SystemExit):
        tideforge.render(recipe, 'alma10')


def test_target_merge_preserves_common_environment_and_recipe():
    recipe = load('cosmic-bg')
    selected = tideforge.target_recipe(recipe, 'alma10')
    assert selected['build']['environment']['VERGEN_GIT_SHA'] == recipe['build']['environment']['VERGEN_GIT_SHA']
    assert selected['build']['environment']['PATH'].startswith('/opt/tunaos/')
    assert 'PATH' not in recipe['build']['environment']
