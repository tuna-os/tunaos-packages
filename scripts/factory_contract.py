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
import json
import re

# Inert for every format: nothing in any build or verify path reads these.
#   r2_path / r2_path_aarch64  bucket WRITE paths, read by the publishers and
#                              scripts/generate-distributed-workflow.py
#   gap_measurement            read only by scripts/gap_engine.py
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


def consumer_binding_inputs(bindings: Any, factory: dict, target_id: str, architecture: str) -> list[dict]:
    """Pin this cell's consumers without importing another target's inputs."""
    digest = re.compile(r"sha256:[0-9a-f]{64}\Z")
    fields = {"target", "sourceRevision", "contractDigest", "baseDigest", "baseReference", "approvedSources"}
    target_fields = {"variant", "flavor", "platform", "cpuBaseline", "hardwareScope"}
    if not isinstance(bindings, list):
        raise ValueError("consumer bindings must be an array")
    seen = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != fields:
            raise ValueError("invalid consumer binding shape")
        target = binding["target"]
        if not isinstance(target, dict) or set(target) != target_fields:
            raise ValueError("invalid consumer binding target")
        if any(not isinstance(value, str) or not value for value in target.values()):
            raise ValueError("consumer binding target fields must be strings")
        identifier = r"[a-z0-9][a-z0-9._-]*"
        if any(not re.fullmatch(identifier, target[field]) for field in ("variant", "flavor")):
            raise ValueError("invalid consumer target identifier")
        tokens = target["flavor"].split("-")
        scope = "apple-silicon" if "asahi" in tokens else "apple-t2" if "t2" in tokens else "generic"
        platform = target["platform"]
        baseline = ("armv8-a" if platform == "linux/arm64" else
                    "x86-64-v2" if platform == "linux/amd64/v2" else
                    "x86-64-v3" if target["variant"] in {"skipjack", "wahoo"} else "x86-64")
        if (platform not in {"linux/amd64", "linux/amd64/v2", "linux/arm64"}
            or target["hardwareScope"] != scope or target["cpuBaseline"] != baseline
            or ("asahi" in tokens and "t2" in tokens)
            or (scope == "apple-silicon" and platform != "linux/arm64")
            or (scope == "apple-t2" and platform == "linux/arm64")
            or (platform == "linux/amd64/v2" and target["variant"] not in {"albacore", "yellowfin"})
            or (platform == "linux/amd64" and target["variant"] in {"albacore", "yellowfin"})):
            raise ValueError("inconsistent consumer hardware or CPU identity")
        if not isinstance(binding["sourceRevision"], str) or not re.fullmatch(r"[0-9a-f]{40}", binding["sourceRevision"]):
            raise ValueError("consumer binding revision must be immutable")
        if any(not isinstance(binding[field], str) or not digest.fullmatch(binding[field]) for field in ("contractDigest", "baseDigest")):
            raise ValueError("consumer binding digests must be immutable")
        if not isinstance(binding["baseReference"], str) or binding["baseReference"].rsplit("@", 1)[-1] != binding["baseDigest"]:
            raise ValueError("consumer binding base reference disagrees")
        if not isinstance(binding["approvedSources"], list):
            raise ValueError("consumer binding sources must be an array")
        if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", binding["baseReference"]):
            raise ValueError("invalid immutable base reference")
        for source in binding["approvedSources"]:
            if (not isinstance(source, dict)
                or not {"id", "url", "signingIdentity"} <= set(source)
                or set(source) - {"id", "url", "signingIdentity", "snapshotDigest"}
                or not isinstance(source["id"], str) or not re.fullmatch(identifier, source["id"])
                or not isinstance(source["url"], str) or not re.fullmatch(r"https://[^\s@]+", source["url"])
                or not isinstance(source["signingIdentity"], str) or not source["signingIdentity"]
                or ("snapshotDigest" in source and (not isinstance(source["snapshotDigest"], str)
                    or not digest.fullmatch(source["snapshotDigest"])))):
                raise ValueError("invalid approved source snapshot")
        adapter = factory.get("consumer_adapters", {}).get(target["variant"], {})
        if (adapter.get("target") != target_id
            or adapter.get("architectures", {}).get(target["platform"]) != architecture
            or target["cpuBaseline"] not in adapter.get("cpuBaselines", [])):
            raise ValueError("consumer binding belongs to another factory target or baseline")
        identity = tuple(target[field] for field in ("variant", "flavor", "platform"))
        if identity in seen:
            raise ValueError("duplicate consumer binding")
        seen.add(identity)
    return sorted(bindings, key=lambda item: json.dumps(item, sort_keys=True))
