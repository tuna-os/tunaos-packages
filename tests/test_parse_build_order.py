"""Tests for scripts/parse-build-order.py"""

import os
import subprocess
import sys

import pytest
import yaml

SAMPLE_MANIFEST = {
    'tiers': [
        {
            'name': 'core',
            'packages': [
                {'path': 'src/core/systemd'},
                {'copr_name': 'external-core'},
                {'path': 'src/core/glibc'},
            ],
        },
        {
            'name': 'desktop',
            'packages': [
                {'path': 'src/desktop/gnome-shell'},
                {'path': 'src/desktop/mutter', 'spec_override': 'mutter.spec'},
            ],
        },
    ],
}


def _run_parse_build_order(manifest_path, *args):
    """Run parse-build-order.py with given args and return (stdout, stderr, returncode)."""
    script = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'parse-build-order.py')
    result = subprocess.run(
        [sys.executable, script, str(manifest_path), *args],
        capture_output=True, text=True
    )
    return result.stdout, result.stderr, result.returncode


@pytest.fixture
def cli_manifest(tmp_path):
    manifest_path = tmp_path / 'build-order.yml'
    manifest_path.write_text(yaml.safe_dump(SAMPLE_MANIFEST))
    return manifest_path


def test_all_tab_separated_output(cli_manifest):
    """Preserve manifest order, empty spec columns, and spec overrides."""
    stdout, stderr, rc = _run_parse_build_order(cli_manifest, '--all')

    assert rc == 0
    assert stderr == ''
    assert stdout == (
        'src/core/systemd\t\n'
        'src/core/glibc\t\n'
        'src/desktop/gnome-shell\t\n'
        'src/desktop/mutter\tmutter.spec\n'
    )


@pytest.mark.parametrize(('tiers_filter', 'expected'), [
    ('core', 'src/core/systemd\t\nsrc/core/glibc\t\n'),
    (' desktop ', 'src/desktop/gnome-shell\t\nsrc/desktop/mutter\tmutter.spec\n'),
    (' , core, , missing, ', 'src/core/systemd\t\nsrc/core/glibc\t\n'),
    (' desktop, core ', (
        'src/core/systemd\t\n'
        'src/core/glibc\t\n'
        'src/desktop/gnome-shell\t\n'
        'src/desktop/mutter\tmutter.spec\n'
    )),
    ('nonexistent', ''),
])
def test_all_tiers_filter(cli_manifest, tiers_filter, expected):
    """Select exact tier names, ignoring comma padding and empty names."""
    stdout, stderr, rc = _run_parse_build_order(
        cli_manifest, '--all', '--tiers-filter', tiers_filter,
    )

    assert rc == 0
    assert stderr == ''
    assert stdout == expected


def test_tier_skips_copr_name_only_entries(cli_manifest):
    stdout, stderr, rc = _run_parse_build_order(cli_manifest, '--tier', 'core')

    assert rc == 0
    assert stderr == ''
    assert stdout == 'src/core/systemd\t\nsrc/core/glibc\t\n'


def test_list_all_tiers(tmp_path):
    """Test default output: list all tiers and packages."""
    manifest_path = tmp_path / 'build-order.yml'
    with open(manifest_path, 'w') as f:
        yaml.dump(SAMPLE_MANIFEST, f)

    stdout, stderr, rc = _run_parse_build_order(manifest_path)
    assert rc == 0
    assert '=== core ===' in stdout
    assert '=== desktop ===' in stdout
    assert 'src/core/systemd' in stdout
    assert 'src/core/glibc' in stdout
    assert 'src/desktop/gnome-shell' in stdout
    assert 'src/desktop/mutter' in stdout


def test_list_tier_names(tmp_path):
    """Test --tiers flag: list tier names only."""
    manifest_path = tmp_path / 'build-order.yml'
    with open(manifest_path, 'w') as f:
        yaml.dump(SAMPLE_MANIFEST, f)

    stdout, stderr, rc = _run_parse_build_order(manifest_path, '--tiers')
    assert rc == 0
    lines = stdout.strip().splitlines()
    assert 'core' in lines
    assert 'desktop' in lines
    assert len(lines) == 2


def test_list_specific_tier(tmp_path):
    """Test --tier flag: list packages for a specific tier."""
    manifest_path = tmp_path / 'build-order.yml'
    with open(manifest_path, 'w') as f:
        yaml.dump(SAMPLE_MANIFEST, f)

    stdout, stderr, rc = _run_parse_build_order(manifest_path, '--tier', 'core')
    assert rc == 0
    assert 'src/core/systemd' in stdout
    assert 'src/core/glibc' in stdout
    assert 'src/desktop/gnome-shell' not in stdout


def test_spec_override_shown(tmp_path):
    """Test that spec_override is displayed correctly."""
    manifest_path = tmp_path / 'build-order.yml'
    with open(manifest_path, 'w') as f:
        yaml.dump(SAMPLE_MANIFEST, f)

    stdout, stderr, rc = _run_parse_build_order(manifest_path, '--tier', 'desktop')
    assert rc == 0
    assert 'src/desktop/mutter' in stdout
    # spec_override should appear
    assert 'mutter.spec' in stdout


def test_tier_not_found(tmp_path):
    """Test error message for nonexistent tier."""
    manifest_path = tmp_path / 'build-order.yml'
    with open(manifest_path, 'w') as f:
        yaml.dump(SAMPLE_MANIFEST, f)

    stdout, stderr, rc = _run_parse_build_order(manifest_path, '--tier', 'nonexistent')
    assert rc == 1
    assert 'not found' in stderr


def test_empty_manifest(tmp_path):
    """Test behavior with empty manifest."""
    manifest_path = tmp_path / 'build-order.yml'
    with open(manifest_path, 'w') as f:
        yaml.dump({'tiers': []}, f)

    stdout, stderr, rc = _run_parse_build_order(manifest_path)
    assert rc == 0
    assert stdout.strip() == ''


def test_missing_manifest(tmp_path):
    """Test error with nonexistent manifest."""
    manifest_path = tmp_path / 'nonexistent.yml'

    stdout, stderr, rc = _run_parse_build_order(manifest_path)
    assert rc != 0
    assert 'No such file' in stderr or 'not found' in stderr
