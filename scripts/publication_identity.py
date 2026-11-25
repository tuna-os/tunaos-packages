"""Format-qualified publication identity and same-identity digest conflicts.

Evaluated from frostyard/repogen (internal/utils/package_identity.go,
MIT-style): a logical package identity, plus rejection of changed bytes
published under an identity that already exists. Adapted, not adopted:

* The identity is qualified by (format, target, arch, kind) so equal names
  in different formats, targets or architectures never collide. Musl and
  glibc targets are different `target` values and are never merged.
* `evr` is an opaque string compared only for equality here. Ordering stays
  with the native comparators in repo_index.py; no cross-distro ordering.
* Exact re-publication (same identity, same digest) is idempotent. Same
  identity with a different digest is a conflict: the fix is a version
  bump, not an overwrite.
"""
from __future__ import annotations

import collections
from collections.abc import Iterable
from typing import NamedTuple

KINDS = ("binary", "source")


class PackageIdentity(NamedTuple):
    format: str
    target: str
    arch: str
    kind: str
    name: str
    evr: str


def identity(fmt: str, target: str, row: dict, kind: str | None = None) -> PackageIdentity:
    """Identity of one index row (the shape repo_index.iter_rows yields)."""
    if kind is None:
        kind = "source" if row.get("arch") == "src" else "binary"
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    return PackageIdentity(fmt, target, row.get("arch") or "", kind,
                           row["name"], row["evr"])


def detect_digest_conflicts(
        entries: Iterable[tuple[PackageIdentity, str]]) -> list[dict]:
    """Identities published with more than one distinct digest.

    `entries` is (identity, digest) pairs. Repeating an identity with the
    same digest is a no-op; only differing bytes are reported.
    """
    digests: dict[PackageIdentity, set[str]] = collections.defaultdict(set)
    for ident, digest in entries:
        digests[ident].add(digest)
    return [{"identity": ident._asdict(), "digests": sorted(found)}
            for ident, found in sorted(digests.items())
            if len(found) > 1]


def admit(published: dict[PackageIdentity, str],
          wave: Iterable[tuple[PackageIdentity, str]]) -> list[dict]:
    """Conflicts a wave would create against already-published bytes."""
    return detect_digest_conflicts(
        [(i, d) for i, d in published.items()] + list(wave))
