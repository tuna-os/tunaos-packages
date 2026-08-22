"""A cell may not name an architecture its target contract does not declare.

This is the check that was missing when xfce-fedora-aarch64 was defined. The
cell was added, its mock config written, its tests passing -- and every gate
run since failed within a tenth of a second of reaching the action key:

    aarch64 is not declared for target fedora

`manifests/package-factory.yaml`'s `architectures:` is the authority, and
scripts/tideforge-action-cache.py enforces it before a key can be computed. So
a cell naming an undeclared arch is not merely unconventional, it is work the
factory refuses to key -- and the failure surfaces only on a runner, in CI, on
whatever branch happens to carry it.

Every test written for that cell asserted things about the cell: that it
existed, that it ran on an arm runner, that its mock config targeted aarch64.
None crossed the manifest boundary to the contract, so none of them could see
the one fact that mattered. This one does.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILDS = ROOT / "manifests" / "package-builds.yaml"
FACTORY = ROOT / "manifests" / "package-factory.yaml"


def targets() -> dict:
    return yaml.safe_load(FACTORY.read_text(encoding="utf-8"))["targets"]


def cells() -> list[dict]:
    manifest = yaml.safe_load(BUILDS.read_text(encoding="utf-8"))
    return [
        cell for cell in manifest["native_builds"]
        if cell.get("enabled", True) is not False
    ]


def test_every_cell_names_a_target_that_exists():
    known = set(targets())
    for cell in cells():
        assert cell["target"] in known, f"{cell['id']}: unknown target {cell['target']}"


def test_every_cell_architecture_is_declared_by_its_target():
    """The assertion that would have caught xfce-fedora-aarch64 at commit time
    rather than on a runner."""
    declared = {name: set(spec.get("architectures") or []) for name, spec in targets().items()}
    violations = [
        (cell["id"], cell["target"], cell["architecture"], sorted(declared[cell["target"]]))
        for cell in cells()
        if cell["architecture"] not in declared[cell["target"]]
    ]
    assert not violations, violations


def test_the_check_is_not_vacuous():
    """A guard over an empty set passes for the wrong reason."""
    assert len(cells()) >= 10
    assert any(cell["architecture"] == "aarch64" for cell in cells())


def test_no_target_declares_an_architecture_twice():
    for name, spec in targets().items():
        arches = spec.get("architectures") or []
        assert len(arches) == len(set(arches)), name
