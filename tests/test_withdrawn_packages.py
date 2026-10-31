"""Unit tests for scripts/withdrawn_packages.py.

Verifies manifest loading, the exact-(name, evr) matching rule, and the
createrepo_c exclude globs it computes for scripts/plan-withdrawn-excludes.py
and scripts/check-upstream-shadowing.py.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "withdrawn_packages", ROOT / "scripts" / "withdrawn_packages.py")
assert SPEC and SPEC.loader
wp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wp)


def test_load_reads_the_real_manifest() -> None:
    """The seeded manifest (#525) is schema 1 and parses to a dict of lists."""
    targets = wp.load()
    assert "hummingbird" in targets
    assert all(isinstance(e, dict) and {"name", "evr"} <= e.keys()
               for e in targets["hummingbird"])


def test_the_real_manifest_declares_only_real_targets() -> None:
    """A typo'd target key (e.g. "hummingbrid") would load without error
    and silently withdraw nothing anywhere -- the check would go quiet for
    the wrong reason."""
    factory = yaml.safe_load(
        (ROOT / "manifests" / "package-factory.yaml").read_text(encoding="utf-8"))
    declared = set(factory.get("targets") or {})
    for target in wp.load():
        assert target in declared, (
            f"manifests/withdrawn-packages.yaml declares {target!r}, which "
            "is not a target in manifests/package-factory.yaml"
        )


def test_the_real_manifest_has_no_duplicate_entries() -> None:
    """Two entries for the same (target, name, evr) is a copy-paste slip --
    harmless to createrepo_c (the glob just matches twice) but a sign the
    manifest was edited by hand without checking what was already there."""
    for target, entries in wp.load().items():
        seen = set()
        for entry in entries:
            key = (entry.get("name"), entry.get("evr"))
            assert key not in seen, f"{target}: duplicate entry for {key}"
            seen.add(key)


def test_the_real_manifest_entries_are_glob_safe() -> None:
    """excludes_for() wraps name-evr in a glob with no escaping; a name or
    evr containing *, ?, [ or ] would make the pattern match something
    other than what the entry says, or fail to match anything at all."""
    for target, entries in wp.load().items():
        for entry in entries:
            for field in ("name", "evr"):
                value = str(entry.get(field, ""))
                assert not set(value) & set("*?[]"), (
                    f"{target}: {field}={value!r} contains a glob "
                    "metacharacter"
                )


def test_load_a_missing_file_is_nothing_withdrawn(tmp_path) -> None:
    assert wp.load(tmp_path / "does-not-exist.yaml") == {}


def test_load_rejects_an_unknown_schema(tmp_path) -> None:
    """A future-schema manifest must not be silently misread as empty-safe
    or, worse, matched against the wrong field names."""
    path = tmp_path / "withdrawn.yaml"
    path.write_text("schema: 2\ntargets: {hummingbird: [{name: sudo, evr: '1'}]}\n")
    assert wp.load(path) == {}


def test_is_withdrawn_matches_name_and_evr_exactly() -> None:
    targets = {
        "hummingbird": [
            {"name": "sudo", "evr": "1.9.17-1.p2.fc43", "reason": "..."},
        ],
    }
    assert wp.is_withdrawn(targets, "hummingbird", "sudo", "1.9.17-1.p2.fc43")
    # A different target's withdrawal must not leak into this one.
    assert not wp.is_withdrawn(targets, "gnome50", "sudo", "1.9.17-1.p2.fc43")
    # A different package under the same name must not match.
    assert not wp.is_withdrawn(targets, "hummingbird", "sudo-other", "1.9.17-1.p2.fc43")


def test_is_withdrawn_does_not_match_a_different_evr() -> None:
    """A future rebuild at a new evr -- upstream dropping the package, or us
    legitimately catching back up -- must serve normally, not inherit an old
    entry's withdrawal by bare package name."""
    targets = {"hummingbird": [{"name": "sudo", "evr": "1.9.17-1.p2.fc43"}]}
    assert not wp.is_withdrawn(targets, "hummingbird", "sudo", "1.9.17-2.p2.fc43")


def test_excludes_for_builds_one_glob_per_entry() -> None:
    targets = {
        "hummingbird": [
            {"name": "sudo", "evr": "1.9.17-1.p2.fc43"},
            {"name": "bubblewrap", "evr": "0.11.2-2.1.fc43"},
        ],
        "other-target": [{"name": "unrelated", "evr": "1-1"}],
    }
    globs = wp.excludes_for(targets, "hummingbird")
    assert globs == sorted([
        "*sudo-1.9.17-1.p2.fc43.*.rpm",
        "*bubblewrap-0.11.2-2.1.fc43.*.rpm",
    ])
    # Only the requested target's entries -- a shared file must not leak
    # one target's withdrawal into another target's publish.
    assert "*unrelated-1-1.*.rpm" not in globs


def test_excludes_for_an_undeclared_target_is_empty() -> None:
    assert wp.excludes_for({"hummingbird": [{"name": "sudo", "evr": "1"}]},
                            "gnome50") == []


@pytest.mark.parametrize("entry", [
    {"evr": "1.0-1"},                 # no name
    {"name": "sudo"},                 # no evr
    "not-a-mapping",                  # wrong shape entirely
])
def test_excludes_for_skips_malformed_entries(entry) -> None:
    assert wp.excludes_for({"hummingbird": [entry]}, "hummingbird") == []
