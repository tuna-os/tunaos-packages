"""A wedged mock build must fail visibly, not hold the tier hostage.

Runs 31732589290 and 31757583258 each sat 2+ hours with ZERO further log
output after netcdf's builddep retry entered mock chroot init. The runner
agent stayed healthy the whole time -- it was the podman container that
never returned -- so GitHub's step timeout never fired, the workflow's
concurrency group stayed locked (queueing every later dispatch behind the
zombie), and both runs ended only when a human cancelled them.

An external timeout(1) around the container converts that hang into an
ordinary per-package failure (exit 124) that the existing tier-retry logic
skips past, the same external-bound pattern tunaOS#1572 applied to the
syft scan for the same reason: the process that hangs cannot be the one
enforcing its own deadline.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lib" / "build-chain" / "podman.sh"


def _mock_container_fn() -> str:
    """The real _run_mock_container body, not a reimplementation."""
    text = SCRIPT.read_text()
    match = re.search(
        r"_run_mock_container\(\)\s*\{.*?\n    \}", text, re.S
    )
    assert match, "_run_mock_container not found in build-chain.sh"
    return match.group(0)


def test_the_container_run_is_time_bounded() -> None:
    fn = _mock_container_fn()
    assert re.search(r"timeout\s+.*podman run", fn, re.S), (
        "_run_mock_container invokes podman with no external time bound, so "
        "a wedged mock hangs the tier silently until a human cancels the "
        "job -- the exact failure of runs 31732589290 and 31757583258"
    )


def test_the_bound_is_overridable_but_has_a_default() -> None:
    """A hard-coded bound would need a code change for a known-slow rebuild;
    an env var with no default protects nothing on the runs that matter."""
    fn = _mock_container_fn()
    assert re.search(r"\$\{MOCK_TIMEOUT_MINUTES:-\d+\}", fn), (
        "the timeout is not MOCK_TIMEOUT_MINUTES with a numeric default"
    )


def test_a_stuck_container_is_killed_not_just_asked() -> None:
    fn = _mock_container_fn()
    assert "--kill-after" in fn, (
        "timeout has no --kill-after, so a container that ignores SIGTERM "
        "still hangs forever"
    )


def test_the_reason_travels_with_the_code() -> None:
    """An unexplained timeout wrapper reads as paranoia and invites removal."""
    text = SCRIPT.read_text()
    assert "31732589290" in text or "31757583258" in text, (
        "the script does not cite the hung runs that motivated the bound"
    )
