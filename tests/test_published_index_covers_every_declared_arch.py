"""A target that declares an architecture must declare an index for it.

`published_index` is resolved PER ARCH by scripts/published_index.py. A target
that declares two architectures but only one index key therefore hands the
other arch an EMPTY PUBLISHED_INDEX -- no apt/dnf source is written at all, and
every factory-built dependency reads as missing.

That is not hypothetical. ubuntu and debian each declared
`architectures: [amd64, arm64]` with an amd64-only index, so
tideforge-quickshell-ubuntu-arm64 reported

    libcpptrace-dev              NOT AVAILABLE

against a package that was built, published and served for arm64. It looked
like a recipe problem for several rounds. The producer half of the same gap was
publish-tideforge-debs.yml having no arch dimension at all, so nothing arm64
was ever published either.

el10 and hummingbird already declared every arch, which is what made the
omission easy to miss by reading: the file looks consistent until you compare
the two lists key by key.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifests" / "package-factory.yaml"


@pytest.fixture(scope="module")
def targets() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["targets"]


def test_every_declared_arch_has_an_index_where_one_is_declared(targets):
    """Only applies to targets that HAVE a published index: arch and
    opensuse-tumbleweed legitimately have none, and requiring one there would
    invent a repo that does not exist.

    `published_index_pending` exempts an arch that has no index BY DESIGN --
    a new arch whose prefix does not exist until the first wave writes it, and
    for which a declared URL would 404. That is a different state from the
    omission this test was written for, and the manifest could not previously
    express the difference: fedora gained an aarch64 cell, so it had to declare
    the arch, and declaring xfce/44-aarch64 before anything published there
    would have been a lie. An arch that is neither indexed nor listed pending
    still fails."""
    for name, target in targets.items():
        index = target.get("published_index")
        if not index:
            continue
        pending = set(target.get("published_index_pending") or [])
        missing = [
            a for a in target.get("architectures", [])
            if a not in index and a not in pending
        ]
        assert not missing, (name, missing, sorted(index))


def test_a_pending_arch_is_one_the_target_actually_builds(targets):
    """Dead configuration otherwise -- the same drift the converse test below
    guards against."""
    for name, target in targets.items():
        pending = target.get("published_index_pending") or []
        unknown = [a for a in pending if a not in (target.get("architectures") or [])]
        assert not unknown, (name, unknown)


def test_nothing_is_both_pending_and_indexed(targets):
    """Pending means "no index yet". Once the URL is declared the entry must
    go, or the exemption outlives the reason for it and silently covers a
    later omission."""
    for name, target in targets.items():
        pending = set(target.get("published_index_pending") or [])
        indexed = set(target.get("published_index") or {})
        assert not (pending & indexed), (name, sorted(pending & indexed))


def test_pending_is_inert_for_builds(targets):
    """It must not re-key every cell on the target. Nothing in any build or
    verify path reads it -- published_index.py reads published_index only."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import factory_contract

    assert "published_index_pending" in factory_contract.BUILD_INERT_KEYS
    for target in targets.values():
        assert "published_index_pending" not in factory_contract.build_view(target)


def test_the_index_declares_no_arch_the_target_does_not_build(targets):
    """The converse: an index key for an arch the target never builds is dead
    configuration that will drift silently."""
    for name, target in targets.items():
        index = target.get("published_index") or {}
        extra = [a for a in index if a not in target.get("architectures", [])]
        assert not extra, (name, extra)


def test_the_deb_targets_point_both_arches_at_the_same_flat_repo(targets):
    """A flat apt repo serves every architecture from ONE URL -- one pool, one
    Packages, apt selecting on the Architecture field. Giving arm64 a separate
    URL would invent a repository the publisher does not write."""
    for name in ("ubuntu", "debian"):
        index = targets[name]["published_index"]
        assert index["amd64"] == index["arm64"], (name, index)
