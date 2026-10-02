"""The two ways a shared mock root cache can cost more than it saves.

Neither is exercised by CI before it runs for real: the action key is derived
from the manifest, the source paths and the image digest, so editing
build-chain.sh does not invalidate a single cell and every build-chain check on
a PR cache-hits.  The first execution of this code is a nightly.  So the two
failure modes are handled by construction and pinned here.

PERMISSIONS.  Only <config>/root_cache is bind-mounted, so podman creates the
<config> parent itself -- root-owned, 0755 -- where in the image
/var/cache/mock is root:mock 2775 and mock builds the whole subtree itself.
mock runs as builder:mock, so an unwritable parent stops it creating the
siblings it expects (yum_cache) and the mount that was meant to save a third of
the run breaks every package in it.

TRUNCATION.  A tarball cut short by a kill is worse than no tarball at all:
every later package in the job fails to unpack it.  The guard that discards one
must not touch a file another worker is writing at that moment -- testing an
in-flight write reads as corrupt, and with several workers the delete/rebuild
ping-pong means the cache never survives to be used and the optimisation
silently does nothing.  That is what the age gate is for, and an age gate is
exactly the kind of condition that looks decorative and gets dropped.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAIN = ROOT / "scripts" / "build-chain.sh"


def guard_source() -> str:
    """The corrupt-tarball guard, lifted verbatim out of build-chain.sh."""
    lines = CHAIN.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "_root_cache_tarball=" in line)
    end = next(i for i, line in enumerate(lines[start:], start) if line.strip() == "fi")
    return textwrap.dedent("\n".join(lines[start:end + 1]))


def run_guard(tmp_path: Path, contents: bytes, minutes_old: int) -> bool:
    """Returns whether the tarball survived."""
    cache = tmp_path / "hummingbird-ci" / "root_cache"
    cache.mkdir(parents=True)
    tarball = cache / "cache.tar.gz"
    tarball.write_bytes(contents)
    subprocess.run(["touch", "-d", f"-{minutes_old} minutes", str(tarball)], check=True)
    script = "\n".join([
        "set -euo pipefail",
        f'MOCK_CACHE_DIR={tmp_path}',
        "MOCK_CONFIG=hummingbird-ci",
        "log() { echo \"$@\"; }",
        guard_source(),
    ])
    subprocess.run(["bash", "-c", script], check=True, capture_output=True)
    return tarball.exists()


VALID_GZIP = subprocess.run(
    ["gzip", "-c"], input=b"buildroot", capture_output=True, check=True).stdout


def test_a_stale_corrupt_tarball_is_discarded(tmp_path: Path) -> None:
    assert not run_guard(tmp_path, VALID_GZIP[:12], minutes_old=30), (
        "a truncated root cache survived; every package after it in the job "
        "would fail to unpack it"
    )


def test_a_tarball_another_worker_is_writing_is_left_alone(tmp_path: Path) -> None:
    assert run_guard(tmp_path, VALID_GZIP[:12], minutes_old=0), (
        "the guard deleted a cache that is being written right now. Concurrent "
        "workers would then delete each other's rebuilds forever and the cache "
        "would never be used -- silently, since nothing fails"
    )


def test_a_good_tarball_is_kept(tmp_path: Path) -> None:
    assert run_guard(tmp_path, VALID_GZIP, minutes_old=30), (
        "the guard discarded a valid root cache, which is the 34% it exists "
        "to save"
    )


def test_the_mount_point_and_its_parent_are_made_writable() -> None:
    body = CHAIN.read_text(encoding="utf-8")
    chmods = [line for line in body.splitlines()
              if "chmod" in line and "/var/cache/mock/" in line]
    assert chmods, (
        "nothing makes /var/cache/mock/<config> writable inside the container. "
        "podman creates that parent root-owned when it creates the mount "
        "point, and mock runs as builder:mock"
    )
