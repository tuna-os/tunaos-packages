#!/usr/bin/env python3
"""Stage build-chain RPM artifacts in ORAS so independent builds don't race the publish repo.

publish-build-chain-rpms.yml's `rclone sync` into R2 must be serialized --
two of them running at once can delete each other's packages (#124 /
INCIDENT-repo-wipe-gnome), which is why that workflow's concurrency group
allows only one publisher at a time. Building has no such constraint: many
independent package builds (a nightly run, a one-off dispatch for a single
missing package, a manual retry) can run concurrently without conflict, as
long as they don't all try to be the one process that syncs to R2.

This module is the middle layer that lets them not have to be. Every build
job pushes its own successfully-built RPMs here, as independent ORAS
artifacts on ghcr.io -- concurrent pushes of different tags don't race each
other the way `rclone sync` does. The single serialized publish job then
pulls everything currently staged before its normal createrepo+sync, so
publishing itself is exactly as serialized as before; only the building
that feeds it is free to fan out.

push <dir>: push every *.rpm in <dir> to the shared ORAS repo, tagged by a
sanitized NVR. RPM release fields can carry `~` (prerelease, e.g. 51~beta)
and `^` (snapshot/post-release), neither of which is a valid OCI tag
character -- collapsed to `_` here. Nothing is actually lost by that: the
pushed file keeps its real name via ORAS's own title annotation regardless
of what the tag looks like, and the tag only needs to be unique and valid,
not reversible. (Two distinct RPM names collapsing to the identical
sanitized tag is possible in principle -- it would need two builds to
differ ONLY in a character this collapses, in the same position -- but
that is not a pattern real RPM naming produces, and see the module
docstring's incident note for why the collision that actually matters,
concurrent *publish*, is guarded elsewhere.)

pull-all <dir> --dist D --arch A: pull every staged tag that belongs to one
cell -- its %{dist} (.bfin1, .el10, ...) and its architecture or noarch --
into <dir>, restoring original filenames. The ORAS repository is shared by
every build-chain family, and the publisher signs and places everything it
pulls, so an unscoped pull put one family's RPMs into another's prefix
(345 staged .el10 RPMs were one hummingbird publish away from
hummingbird/20251124-x86_64, tuna-os/tunaos-packages#832). Best-effort per tag: one vanished or
corrupt entry must not stop the publish job from picking up everything
else that's staged.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ORAS_REPO = "ghcr.io/tuna-os/build-chain-artifacts"
# Opt-in, like tideforge_cache.ORAS_PUSH_ENABLED: a staging push must never
# be what makes a local or test run try to reach the network.
ORAS_PUSH_ENABLED = os.environ.get("BUILD_CHAIN_ORAS_PUSH", "") == "1"

_UNSAFE_TAG_CHARS = re.compile(r"[^a-zA-Z0-9._-]")


def sanitize_tag(rpm_name: str) -> str:
    """Map an RPM filename to a valid OCI tag (see module docstring)."""
    stem = rpm_name.removesuffix(".rpm")
    tag = _UNSAFE_TAG_CHARS.sub("_", stem)
    if not tag or not (tag[0].isalnum() or tag[0] == "_"):
        tag = f"x{tag}"
    return tag


def push(artifacts_dir: Path) -> int:
    """Push every *.rpm in artifacts_dir to the shared ORAS repo.

    Best-effort like tideforge_cache.oras_push: a staging push must never
    fail the build that produced real, already-verified RPMs. Returns the
    count of RPMs successfully staged.
    """
    if not ORAS_PUSH_ENABLED:
        return 0
    pushed = 0
    for rpm in sorted(artifacts_dir.glob("*.rpm")):
        tag = f"{ORAS_REPO}:{sanitize_tag(rpm.name)}"
        try:
            subprocess.run(
                ["oras", "push", "--plain-http=false", tag, str(rpm)],
                check=True, capture_output=True, text=True, timeout=120,
            )
            pushed += 1
        except (subprocess.CalledProcessError, FileNotFoundError,
                subprocess.TimeoutExpired, OSError) as exc:
            print(f"oras-stage-artifacts: push failed for {rpm.name}: {exc}", file=sys.stderr)
    return pushed


def list_tags() -> list[str]:
    """List every tag currently staged. Empty on any failure -- an
    unreachable registry or an empty repo both mean "nothing to pull",
    not an error the caller should propagate."""
    try:
        result = subprocess.run(
            ["oras", "repo", "tags", "--plain-http=false", ORAS_REPO],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except (subprocess.CalledProcessError, FileNotFoundError,
            subprocess.TimeoutExpired, OSError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def belongs_to(tag: str, dist: str, arch: str) -> bool:
    """Whether a staged tag (sanitized `<name>-<version>-<release>.<arch>`)
    is an RPM of the cell with this %{dist} and architecture."""
    stem, _, tag_arch = tag.rpartition(".")
    if tag_arch not in (arch, "noarch"):
        return False
    release = stem.rpartition("-")[2]
    dist = _UNSAFE_TAG_CHARS.sub("_", dist)
    return release.endswith(dist) or f"{dist}." in release


def pull_all(output_dir: Path, dist: str, arch: str) -> int:
    """Pull every staged artifact of one cell (see belongs_to) into output_dir.

    Returns the count of tags successfully pulled.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pulled = 0
    for tag in list_tags():
        if not belongs_to(tag, dist, arch):
            continue
        try:
            subprocess.run(
                ["oras", "pull", "--plain-http=false", f"{ORAS_REPO}:{tag}",
                 "--output", str(output_dir)],
                check=True, capture_output=True, text=True, timeout=120,
            )
            pulled += 1
        except (subprocess.CalledProcessError, FileNotFoundError,
                subprocess.TimeoutExpired, OSError) as exc:
            print(f"oras-stage-artifacts: pull failed for tag {tag}: {exc}", file=sys.stderr)
    return pulled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    push_parser = subparsers.add_parser("push", help="stage every *.rpm in a directory")
    push_parser.add_argument("artifacts_dir", type=Path)

    pull_parser = subparsers.add_parser("pull-all", help="pull every currently-staged artifact")
    pull_parser.add_argument("output_dir", type=Path)
    pull_parser.add_argument("--dist", required=True,
                             help="the cell's %%{dist}, e.g. .bfin1")
    pull_parser.add_argument("--arch", required=True,
                             help="the cell's architecture; noarch is always included")

    args = parser.parse_args()
    if args.command == "push":
        count = push(args.artifacts_dir)
        print(f"staged {count} RPM(s) to {ORAS_REPO}")
    elif args.command == "pull-all":
        count = pull_all(args.output_dir, args.dist, args.arch)
        print(f"pulled {count} staged artifact(s) from {ORAS_REPO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
