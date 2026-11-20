"""The sync-down guard, run as a script against a stub rclone.

scripts/r2-sync-down.sh is the one place the publishers' sync-down guard
lives: exit 3 is a first publish, any other failure stops the job before the
destructive sync-up, and a populated prefix can refuse a suspiciously small
download. See the script header for the incidents behind each rule.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "r2-sync-down.sh"


@pytest.fixture
def rclone(tmp_path):
    """A stub rclone: logs its argv, drops STUB_FILES files into the dest, exits STUB_RC."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "rclone"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$STUB_LOG"\n'
        'dest="$3"\n'
        'i=0; while [ "$i" -lt "${STUB_FILES:-0}" ]; do\n'
        '  mkdir -p "$dest/sub"; : > "$dest/sub/pkg-$i.rpm"; i=$((i+1))\n'
        "done\n"
        'exit "${STUB_RC:-0}"\n'
    )
    stub.chmod(0o755)
    log = tmp_path / "rclone.log"

    def run(*args, rc=0, files=0):
        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "STUB_RC": str(rc),
            "STUB_FILES": str(files),
        }
        result = subprocess.run(
            ["bash", str(SCRIPT), "--src", "r2:bucket/prefix/", "--dest",
             str(tmp_path / "repo"), *args],
            env=env, capture_output=True, text=True, check=False,
        )
        result.log = log.read_text() if log.exists() else ""
        return result

    return run


def test_a_clean_sync_down_continues(rclone):
    result = rclone(files=2)
    assert result.returncode == 0, result.stderr
    assert "synced down 2 file(s)" in result.stdout


def test_exit_3_is_a_first_publish_not_a_failure(rclone):
    result = rclone(rc=3)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "treating as first publish" in result.stdout


@pytest.mark.parametrize("rc", [1, 2, 4, 5, 7])
def test_any_other_failure_stops_before_the_sync_up(rclone, rc):
    result = rclone(rc=rc, files=5)
    assert result.returncode == 1
    assert f"rclone rc={rc}" in result.stdout


def test_a_floor_refuses_a_suspiciously_small_download(rclone):
    result = rclone("--min-files", "3", "--pattern", "*.rpm", files=2)
    assert result.returncode == 1
    assert "only 2 file(s) matching *.rpm" in result.stdout


def test_a_floor_is_met_by_a_full_download(rclone):
    result = rclone("--min-files", "3", "--pattern", "*.rpm", files=3)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_floor_also_refuses_a_missing_prefix(rclone):
    """A populated prefix that comes back as 'not found' is the worst case of small."""
    result = rclone("--min-files", "1", rc=3)
    assert result.returncode == 1


def test_the_floor_counts_only_the_pattern(rclone):
    result = rclone("--min-files", "1", "--pattern", "*.deb", files=4)
    assert result.returncode == 1


def test_extra_arguments_reach_rclone(rclone):
    result = rclone("--", "--exclude", "repodata/**", files=1)
    assert result.returncode == 0, result.stderr
    assert result.log.strip().endswith("--exclude repodata/**")


@pytest.mark.parametrize("bad", ["", "-1", "ten"])
def test_a_malformed_floor_is_refused(rclone, bad):
    result = rclone("--min-files", bad)
    assert result.returncode == 2
    assert result.log == "", "rclone must not run on a malformed invocation"
