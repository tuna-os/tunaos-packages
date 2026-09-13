"""The catalog covers every package selected by the unified factory."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "manifests" / "catalog.yaml"
FACTORY = ROOT / "manifests" / "package-factory.yaml"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


planner = load("package_factory_planner", ROOT / "scripts" / "plan-package-factory.py")
catalog_builder = load("build_catalog", ROOT / "scripts" / "build-catalog.py")


def catalog() -> list[dict]:
    return (yaml.safe_load(CATALOG.read_text(encoding="utf-8")) or {})["packages"]


def test_committed_catalog_is_generator_fixpoint() -> None:
    assert CATALOG.read_text(encoding="utf-8") == catalog_builder.render_catalog(), (
        "manifests/catalog.yaml is stale; run scripts/build-catalog.py"
    )


def test_catalog_contains_every_executed_package() -> None:
    catalog_names = {entry["name"] for entry in catalog()}
    planned_names = {
        cell["package"] for cell in planner.tideforge_cells(ROOT)
    }
    regenerated_names = {entry["name"] for entry in catalog_builder.collect().values()}
    assert not planned_names - catalog_names
    assert not regenerated_names - catalog_names


def test_all_catalog_targets_are_declared() -> None:
    declared = set((yaml.safe_load(FACTORY.read_text()) or {})["targets"])
    rogue = {
        target
        for entry in catalog()
        for target in entry.get("targets", [])
        if target not in declared
    }
    assert rogue == set()


def test_payload_paths_exist_on_disk() -> None:
    for entry in catalog():
        for package in (entry.get("packaging") or {}).values():
            if not isinstance(package, dict) or package.get("missing_on_disk"):
                continue
            for kind in ("native", "tideforge"):
                if package.get(kind):
                    assert (ROOT / package[kind]).is_dir(), (entry["name"], kind)
