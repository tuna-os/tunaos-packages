"""The container step must hand /builddir back to root on EVERY exit path.

scripts/build-chain.sh runs mock inside a podman container as an unprivileged
`builder` user, over a bind-mounted host directory.  Everything mock writes is
therefore builder-owned, and the HOST process -- the plain CI runner user,
outside the container entirely -- cannot read it through the mount.  The
container has to chown the tree back before it exits.

That restore used to be a plain command placed after the mock invocation.  The
container script runs under `bash -exc`, and mock's failure branch does
`exit 1` INSIDE the `flock -c` string, so the flock command itself fails and
set -e aborts the script right there -- the restore never ran on the failure
path.

Two things broke as a result, one of them silently:

  * A SUCCESSFUL build whose results the host could not enumerate:
        find: /tmp/tmp.XXXXXX/results: Permission denied
        ERROR: No RPMs produced for xfce4-dev-tools
    This is the failure that put the command there originally.

  * The dnf5 already-installed retry (see test_mock_dynamic_br_retry.py) greps
    results/root.log to decide whether to retry.  On a file it cannot read,
    `grep -qs` fails SILENTLY -- the -s is there to swallow exactly that -- so
    the guard fell through to `return 1` and the retry never fired.  Canary run
    31242725235 came back built=24 failed=8, bit-identical to the baseline the
    fix was supposed to move.  Traced directly in that log: the restore appears
    in the set -x output for pytz and rust-matugen, both of which built, and is
    absent for python-wcwidth, which failed.

So the property under test is not "a chown exists somewhere" -- it did, and it
was unreachable.  It is that the restore survives an early exit.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lib" / "build-chain" / "podman.sh"


def container_script() -> str:
    """The bash -exc body podman runs, lifted verbatim from _run_mock_container."""
    text = SCRIPT.read_text()
    match = re.search(
        r"^    _run_mock_container\(\) \{.*?^    \}$", text, re.S | re.M
    )
    assert match, "_run_mock_container not found in build-chain.sh"
    return match.group(0)


def code_lines() -> list[str]:
    """Container script lines with comments stripped.

    Commenting the restore out has to fail these exactly like deleting it.
    """
    return [
        line
        for line in container_script().splitlines()
        if not line.strip().startswith("#")
    ]


def test_ownership_is_restored_from_an_exit_trap() -> None:
    """A trailing command is not enough -- set -e can skip it."""
    traps = [
        line
        for line in code_lines()
        if "trap" in line and "chown -R root:root /builddir" in line and "EXIT" in line
    ]
    assert traps, (
        "the container script never installs an EXIT trap restoring root "
        "ownership of /builddir. Without one, a mock failure aborts the script "
        "at the flock and the host is left unable to read results/root.log."
    )


def test_the_trap_is_installed_before_anything_that_can_fail() -> None:
    """A trap registered after the mock run covers nothing that matters."""
    lines = code_lines()
    trap_at = next(
        i for i, line in enumerate(lines) if "trap" in line and "EXIT" in line
    )
    flock_at = next(i for i, line in enumerate(lines) if "flock" in line)
    assert trap_at < flock_at, (
        f"the EXIT trap is installed at line {trap_at} of the container script, "
        f"after the mock invocation at {flock_at} -- a failure in between exits "
        "with the tree still builder-owned."
    )


def test_no_double_quote_in_the_container_script() -> None:
    """The script is passed as `bash -exc "..."` from the host shell.

    A literal double quote closes that string early; bash then takes the script
    as two arguments, treats the second as $0, and silently drops everything
    after the break.  build-chain.sh carries this warning inline because a
    quoted phrase in a COMMENT once turned the whole container step into a
    no-op -- so the fix above must not reintroduce one.
    """
    body = container_script()
    # The host-side heredoc escapes its own quotes as \" -- those are fine.
    unescaped = re.sub(r'\\"', "", body)
    parts = unescaped.split('bash -exc "', 1)
    assert len(parts) == 2, "container script is no longer a bash -exc string"
    lines = parts[1].splitlines()
    # Drop the string's own terminator -- the last line that is only a quote.
    closing = max(i for i, line in enumerate(lines) if line.strip() == '"')
    offending = [line for line in lines[:closing] if '"' in line]
    assert not offending, f"literal double quote inside the bash -exc string: {offending}"


def test_early_exit_still_runs_the_restore(tmp_path: Path) -> None:
    """The behaviour itself, on the exact shape build-chain.sh uses.

    set -e + a failing `flock -c` whose body exits 1 -- the arrangement that
    skipped the old trailing command.  The marker file stands in for the chown.
    """
    marker = tmp_path / "restored"
    script = f"""
set -ex
trap 'touch {marker!s}' EXIT
flock {tmp_path!s}/lock -c 'exit 1'
echo "unreachable trailing restore"
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode != 0, "the failing flock should abort the script"
    assert "unreachable trailing restore" not in proc.stdout, (
        "set -e did not abort at the flock -- this test no longer models the bug"
    )
    assert marker.exists(), "the EXIT trap did not run on the aborted path"
