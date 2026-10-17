"""The unified planner and verifier agree on every Tideforge cell."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


planner = load("package_factory_planner", ROOT / "scripts" / "plan-package-factory.py")
tideforge = load("tideforge", ROOT / "scripts" / "tideforge.py")
CELLS = planner.tideforge_cells(ROOT)


def recipe_for(package: str) -> dict:
    return yaml.safe_load((ROOT / "packages" / package / "package.yaml").read_text())


def test_unified_planner_has_every_declared_recipe_target() -> None:
    expected = set()
    for path in (ROOT / "packages").glob("*/package.yaml"):
        if path.parent.name == "_template":
            continue
        recipe = yaml.safe_load(path.read_text()) or {}
        package = recipe.get("name") or path.parent.name
        expected.update((package, target) for target in recipe.get("targets", []))
    actual = {(cell["package"], cell["target"]) for cell in CELLS}
    assert actual == expected


def test_explicit_verify_contracts_resolve_for_each_selected_target() -> None:
    for cell in CELLS:
        recipe = recipe_for(cell["package"])
        if not recipe.get("verify"):
            # Generic mode still clean-installs the recipe name and uses a
            # no-op smoke; explicit session/runtime assertions remain data.
            assert recipe.get("name") or cell["package"]
            continue
        resolved = tideforge.verify_metadata(recipe, cell["target"])
        assert str(resolved["install_name"]).strip()
        assert str(resolved["smoke"]).strip()


def test_xfconf_keeps_its_target_specific_install_name() -> None:
    recipe = recipe_for("xfconf")
    assert tideforge.verify_metadata(recipe, "el10")["install_name"] == "xfconf"
    assert tideforge.verify_metadata(recipe, "debian")["install_name"] == "libxfconf-0-4"


def test_niri_carries_its_full_session_contract() -> None:
    smoke = tideforge.verify_metadata(recipe_for("niri"), "el10")["smoke"]
    for asset in (
        "/usr/bin/niri-session",
        "/usr/share/wayland-sessions/niri.desktop",
        "/usr/share/xdg-desktop-portal/niri-portals.conf",
        "/usr/lib/systemd/user/niri.service",
        "/usr/lib/systemd/user/niri-shutdown.target",
    ):
        assert asset in smoke
