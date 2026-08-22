"""A Tumbleweed mirror caught mid-rotation must not turn the gate red.

openSUSE publishes snapshots frequently and download.opensuse.org is a
REDIRECTOR to mirrors that sync at different speeds. A mirror partway through
serves a repomd.xml naming files it has not received yet, and zypper rejects
the whole repository:

    Repository 'openSUSE-Tumbleweed-Oss' is invalid.
    [repo-oss|...] Failed to retrieve new repository metadata.
     - File './repodata/dbe06c23...-appdata-icons.tar.gz' not found on medium
    No provider of 'rpmlint' found.

That failed tideforge-wayland-protocols-opensuse-tumbleweed-x86_64 in run
32586260792 at 16:57:59Z. Measured minutes later: all 20 files repomd.xml
lists resolve 200, and so does that exact appdata-icons.tar.gz. Nothing was
wrong with the repository or with the recipe.

The behaviour is exercised against a fake zypper rather than asserted from the
source text, because the thing that matters is what the loop DOES on the
second attempt -- and the failure mode this guards against (retrying without
clearing the cached repomd, so every attempt re-reads the same index naming
the same absent file) is invisible to a grep for "retry".
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "zypper-refresh-with-retry.sh"
CELL = ROOT / "scripts" / "run-package-factory-cell.sh"
VERIFY = ROOT / "scripts" / "verify-package-factory-cell.sh"
LINT = ROOT / "scripts" / "lint-generated-rpm.sh"


def run_with_fake_zypper(tmp_path: Path, body: str, **env: str) -> subprocess.CompletedProcess:
    """Run the script with `zypper` replaced by a stub that logs its calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    fake = bin_dir / "zypper"
    fake.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "$*" >> {log}
        STATE={tmp_path}/attempts
        {body}
        """),
        encoding="utf-8",
    )
    fake.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ZYPPER_REFRESH_DELAY": "0",
        **env,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=environment
    )
    result.log = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result


REFRESH_FAILS_ONCE = """\
count=$(cat "$STATE" 2>/dev/null || echo 0)
case "$*" in
  *refresh*)
    count=$((count + 1)); echo "$count" > "$STATE"
    if [ "$count" -le 1 ]; then
      echo "File './repodata/abc-appdata-icons.tar.gz' not found on medium" >&2
      exit 6
    fi
    exit 0 ;;
  *) exit 0 ;;
esac
"""

REFRESH_ALWAYS_FAILS = """\
case "$*" in
  *refresh*) echo "some other real error" >&2; exit 6 ;;
  *) exit 0 ;;
esac
"""


@pytest.fixture
def bash_available():
    if not shutil.which("bash"):
        pytest.skip("bash unavailable")


def test_the_script_exists_and_is_executable(bash_available):
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


def test_a_transient_mirror_failure_is_survived(tmp_path, bash_available):
    result = run_with_fake_zypper(tmp_path, REFRESH_FAILS_ONCE)
    assert result.returncode == 0, result.stderr
    assert sum("refresh" in call for call in result.log) == 2


def test_the_cached_metadata_is_cleared_between_attempts(tmp_path, bash_available):
    """The whole point. zypper caches the repomd it already fetched, so a
    retry that does not clear it re-reads the same index naming the same
    absent file and can never succeed."""
    result = run_with_fake_zypper(tmp_path, REFRESH_FAILS_ONCE)
    refresh_at = [i for i, call in enumerate(result.log) if "refresh" in call]
    clean_at = [i for i, call in enumerate(result.log) if "clean" in call and "metadata" in call]
    assert clean_at, f"no metadata clean between attempts: {result.log}"
    assert refresh_at[0] < clean_at[0] < refresh_at[1], result.log


def test_a_real_failure_still_fails(tmp_path, bash_available):
    """Bounded attempts. A retry that never gives up converts a broken
    repository into a 180-minute timeout, which is strictly worse than a fast
    red."""
    result = run_with_fake_zypper(tmp_path, REFRESH_ALWAYS_FAILS, ZYPPER_REFRESH_ATTEMPTS="3")
    assert result.returncode != 0
    assert sum("refresh" in call for call in result.log) == 3


def test_it_succeeds_without_retrying_when_the_mirror_is_healthy(tmp_path, bash_available):
    result = run_with_fake_zypper(tmp_path, "exit 0")
    assert result.returncode == 0
    assert sum("refresh" in call for call in result.log) == 1


def test_the_gpg_auto_import_is_preserved(tmp_path, bash_available):
    """The flag the inline command carried; dropping it would make the first
    refresh prompt and hang instead of failing."""
    result = run_with_fake_zypper(tmp_path, "exit 0")
    assert any("--gpg-auto-import-keys" in call for call in result.log), result.log


def test_no_script_refreshes_zypper_without_retrying():
    """Swept across every script, not just the one I happened to look at first.

    The first fix patched run-package-factory-cell.sh, reasoning from a log
    whose failing step I had not identified. The BUILD was a cache hit that
    run -- `##[notice]hit tideforge-wayland-protocols-opensuse-tumbleweed-x86_64`
    -- so the patched script never executed, and the identical failure
    recurred at a3660e9 with the retry nowhere in the log. The failing step was
    verify-package-factory-cell.sh, in a different file entirely.

    A sweep cannot make that mistake."""
    offenders = []
    for script in sorted((ROOT / "scripts").glob("*.sh")):
        if script.name == "zypper-refresh-with-retry.sh":
            continue
        for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # prose about refreshing is not a refresh
            if "zypper" in stripped and "refresh" in stripped and "retry" not in stripped:
                offenders.append(f"{script.name}:{number}: {stripped}")
    assert not offenders, offenders


def test_the_rpmlint_install_refreshes_first():
    """The step that actually failed. `zypper install` refreshes IMPLICITLY, so
    a mirror mid-rotation surfaces as the thoroughly misleading

        No provider of 'rpmlint' found

    rather than as a network error -- which is part of why the first diagnosis
    landed on the wrong file."""
    text = LINT.read_text(encoding="utf-8")
    retry = text.index("zypper-refresh-with-retry.sh")
    install = text.index("zypper --non-interactive install rpmlint")
    assert retry < install, "the refresh must precede the implicit one"


def test_every_container_running_the_linter_mounts_scripts():
    """The retry lives at /scripts. A caller that does not mount it would skip
    the guard silently, since the call is conditional on the file existing."""
    text = VERIFY.read_text(encoding="utf-8")
    for index, _ in enumerate(text.split("/scripts/lint-generated-rpm.sh")[:-1]):
        block = text.split("/scripts/lint-generated-rpm.sh")[index]
        assert '--volume "$PWD/scripts:/scripts:ro"' in block[-400:], index


def test_the_verify_path_uses_the_retry():
    assert "zypper-refresh-with-retry.sh" in VERIFY.read_text(encoding="utf-8")


def test_the_scripts_directory_is_mounted_for_the_opensuse_container():
    """The script has to be reachable from inside the container, or the cell
    dies on 'No such file or directory' at the first build."""
    text = CELL.read_text(encoding="utf-8")
    opensuse = text[text.index("opensuse-tumbleweed ]]"):]
    opensuse = opensuse[: opensuse.index("rpmbuild -ba")]
    assert '--volume "$PWD/scripts:/scripts:ro"' in opensuse
    assert "/scripts/zypper-refresh-with-retry.sh" in opensuse
