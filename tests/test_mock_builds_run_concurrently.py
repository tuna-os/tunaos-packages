"""Concurrent mock builds must not serialise on the local-repo lock.

`build-chain.sh --jobs N` starts N worker processes per tier.  Every one of
them ran `mock --rebuild` inside `flock /local-repo/repo.lock` -- an EXCLUSIVE
lock -- so the workers took turns doing the single most expensive step in the
run.  `--jobs` selected how many processes waited, not how many built.

The comment justifying it said the builds "share mock chroot initialization".
They do not, on three counts, none of which this change introduced:

  * `--uniqueext=<pkg>` gives every package its own chroot at
    /var/lib/mock/<config>-<pkg>; that is what the flag exists for.
  * /var/lib/mock lives inside the per-build container and dies with it, so
    two concurrent builds cannot observe each other's chroots at all.
  * the one thing concurrent builds do share is the mock ROOT CACHE under
    /var/cache/mock/<config>/root_cache, and mock guards that itself with an
    fcntl lock -- shared to unpack, exclusive to rebuild.  repo.lock never
    protected it and could not have.

What genuinely needs serialising is `createrepo_c --update`, which REWRITES the
metadata mock reads.  That is a classic reader/writer split: builds take the
lock shared, the metadata update takes it exclusive.

These tests pin the split.  Getting it backwards is silent in both directions
-- an exclusive build lock just makes everything slow, and a shared metadata
lock just corrupts a repo occasionally -- so neither shows up as a test failure
anywhere else.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-chain.sh"
MOCK_BACKEND = ROOT / "scripts" / "lib" / "build-chain" / "mock.sh"


def code() -> str:
    """build-chain.sh with comment lines stripped."""
    return "\n".join(
        line
        for line in SCRIPT.read_text().splitlines()
        if not line.strip().startswith("#")
    )


def podman_backend() -> str:
    """build_package_podman only -- the backend the desktop builds use.

    The scope matters. `build_package_mock` runs mock straight on the HOST,
    where /var/lib/mock and /var/cache/mock ARE shared between concurrent
    processes, so its exclusive lock is doing real work and must stay. Only the
    containerised backend gets the isolation that makes a shared lock safe.
    """
    text = "\n".join(
        line
        for line in SCRIPT.read_text().splitlines()
        if not line.strip().startswith("#")
    )
    match = re.search(
        r"^build_package_podman\(\) \{.*?^\}$", text, re.S | re.M
    )
    assert match, "build_package_podman not found in build-chain.sh"
    return match.group(0)


def test_the_mock_build_takes_the_lock_shared() -> None:
    """The expensive step must not exclude its peers."""
    mock_locks = [
        line
        for line in podman_backend().splitlines()
        if "flock" in line and "repo.lock" in line and "createrepo" not in line
    ]
    assert mock_locks, "no flock guarding the containerised mock build found"
    for line in mock_locks:
        assert "flock -s" in line, (
            f"the mock build takes an EXCLUSIVE lock on the local repo: {line.strip()}\n"
            "That serialises every --jobs worker onto one build at a time. "
            "mock only READS the repo; use `flock -s`."
        )


def test_the_host_mock_backend_keeps_its_exclusive_lock() -> None:
    """The other backend has none of the isolation, so it must not be 'fixed' too."""
    text = "\n".join(
        line
        for line in MOCK_BACKEND.read_text().splitlines()
        if not line.strip().startswith("#")
    )
    match = re.search(r"^build_package_mock\(\) \{.*?^\}$", text, re.S | re.M)
    assert match, "build_package_mock not found"
    for line in match.group(0).splitlines():
        if "flock" in line and "repo.lock" in line:
            assert "flock -s" not in line, (
                "build_package_mock runs mock on the HOST, sharing /var/lib/mock "
                "and /var/cache/mock between concurrent processes. Its lock must "
                f"stay exclusive: {line.strip()}"
            )


def test_createrepo_keeps_the_lock_exclusive() -> None:
    """The writer must still exclude everyone, or readers see torn metadata."""
    writer_locks = [
        line
        for line in code().splitlines()
        if "flock" in line and "createrepo_c" in line
    ]
    assert writer_locks, "createrepo_c is no longer called under a lock at all"
    for line in writer_locks:
        assert "flock -s" not in line, (
            f"createrepo_c takes a SHARED lock: {line.strip()}\n"
            "It rewrites the metadata concurrent builds are reading."
        )


def test_every_build_gets_its_own_chroot() -> None:
    """The isolation the shared lock relies on.

    Drop --uniqueext and concurrent builds collide in /var/lib/mock, which is
    the failure the exclusive lock was mistakenly guarding against.
    """
    assert "--uniqueext=" in podman_backend(), (
        "mock is invoked without --uniqueext, so concurrent builds would share "
        "one chroot. Either restore it or restore the exclusive lock."
    )


def test_shared_locks_do_not_exclude_each_other(tmp_path: Path) -> None:
    """The property itself, on real flock.

    Two shared holders run at once; an exclusive one cannot join them.  Guards
    against a future `flock` wrapper or busybox variant where -s is ignored.
    """
    lock = tmp_path / "repo.lock"
    lock.touch()
    script = f"""
set -e
flock -s {lock!s} -c 'sleep 2' &
sleep 0.3
# A second SHARED holder must get in immediately.
timeout 1 flock -s {lock!s} -c 'echo SHARED_OK'
# An EXCLUSIVE holder must not, while the first is still sleeping.
if timeout 1 flock -x {lock!s} -c 'echo EXCLUSIVE_GOT_IN'; then :; fi
wait
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert "SHARED_OK" in proc.stdout, (
        f"a second shared holder was blocked: {proc.stdout!r} {proc.stderr!r}"
    )
    assert "EXCLUSIVE_GOT_IN" not in proc.stdout, (
        "an exclusive holder acquired the lock while a shared one held it"
    )
