#!/usr/bin/env python3
"""One definition of which target-contract fields can change a build.

`manifests/package-factory.yaml` describes each target in a single block, and
that block mixes three different audiences:

  * what a BUILD reads   -- buildroot, probe_image, build_repositories,
                            architectures, format, and (for rpm) the
                            published_index a buildroot adds as a repo;
  * where PUBLISHING writes -- r2_path, r2_path_aarch64;
  * what REPORTING reads -- status, gap_measurement.

Two places decide "did this change matter": scripts/plan-package-factory.py,
which selects cells to run, and scripts/tideforge-action-cache.py, which
computes each cell's content-addressed key. Both must agree, so the answer
lives here rather than in either of them.

## Why this file exists (#473)

The planner already carried a partial version of this idea -- published_index
stripped for deb and pkg.tar.zst, added after declaring the served apt indexes
re-planned every deb cell (run 32397627179). The action key had no
counterpart, so a change to a bucket WRITE path rebuilt every cell on that
target from scratch.

Two readers of one contract, one of them incomplete, is the same shape as the
defect #471 fixed: published_index had two hand-copied readers that both
assumed a string, and fixing one would have left the other wrong. So this is
imported, not duplicated.

## The rule for adding a field here

A field belongs in this set only when NO build and NO verify reads it --
checked by grepping consumers, not by reading the name. published_index is
the field that makes the distinction sharp, and it also shows why the check
must be re-run rather than trusted: it looks like publishing metadata, and
was listed inert for deb on that reading, but the deb buildroot did not
consume it because of a GAP, not by design. #476 closed that gap -- deb now
adds each index as a pinned apt source exactly as rpm adds a yum repo -- so
published_index became a live build input for deb and had to start re-keying
with it. Arch still never looks at it.

The failure this prevents is silent: a cell reusing output built against a
different package universe. When a build path starts reading a field, this
table must move in the same commit.
"""
from __future__ import annotations

from typing import Any

# Inert for every format: nothing in any build or verify path reads these.
#   r2_path / r2_path_aarch64  bucket WRITE paths, read by the publishers and
#                              scripts/generate-distributed-workflow.py
#   gap_measurement            read only by scripts/measure-hummingbird-gap.py
#   status                     a reporting label (supported / scaffold)
BUILD_INERT_KEYS = frozenset({
    "r2_path",
    "r2_path_aarch64",
    "gap_measurement",
    "status",
    # published_index_pending  names arches deliberately without an index yet.
    #                          published_index.py reads published_index and
    #                          nothing else, so no buildroot and no verify can
    #                          see this; it exists to tell the manifest's own
    #                          tests that an absence is a decision rather than
    #                          an omission. Checked by grep, per the rule
    #                          below, not by reading the name.
    "published_index_pending",
})

# Inert for these formats only.
#
# rpm and deb buildroots both ADD published_index as a package source --
# run-package-factory-cell.sh writes /etc/yum.repos.d/tunaos-published.repo
# for rpm and /etc/apt/sources.list.d/tunaos-published-N.list for deb -- so
# for both it is a live build input that must keep re-keying.
#
# Arch is the only format left here: its pkg.tar.zst path has no equivalent
# source and resolves everything from the distro. Adding one would make this
# entry wrong, and the deb entry that used to sit beside it is exactly the
# precedent for noticing.
FORMAT_INERT_KEYS: dict[str, frozenset[str]] = {
    "pkg.tar.zst": frozenset({"published_index"}),
}


def inert_keys(spec: Any) -> frozenset[str]:
    """Fields of this target contract that cannot change a build's output."""
    if not isinstance(spec, dict):
        return BUILD_INERT_KEYS
    return BUILD_INERT_KEYS | FORMAT_INERT_KEYS.get(spec.get("format"), frozenset())


def build_view(spec: Any) -> Any:
    """The target contract as a build sees it, with inert fields removed.

    Non-mappings pass through: a malformed contract must reach the caller
    that validates it, not be silently normalised into an empty dict here.
    """
    if not isinstance(spec, dict):
        return spec
    drop = inert_keys(spec)
    return {key: value for key, value in spec.items() if key not in drop}


def tideforge_cell_id(package: str, target: str, architecture: str) -> str:
    """The identity a tideforge cell works under.

    Both a name and a location: `.factory/<cell_id>/` is where the build
    writes and where the action cache restores to. actions/cache extracts a
    hit to the paths the SAVE recorded, so two workflows that want to share a
    cache entry must agree on this string exactly -- a publisher that invented
    its own `publish-...` prefix would restore a hit into the gate's directory
    and then build in its own, reporting a hit while rebuilding everything
    (#481).

    That makes it the same class of fact as the inert-key table above: two
    readers, and a divergence between them is silent. So it is imported, not
    re-spelled.
    """
    return f"tideforge-{package}-{target}-{architecture}"
