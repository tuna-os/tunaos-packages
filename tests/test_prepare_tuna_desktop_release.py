"""The tuna-desktop recipe tracks one upstream tag, and the fill-in script keeps it that way."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_tuna_desktop_release", ROOT / "scripts" / "prepare-tuna-desktop-release.py"
)
assert SPEC and SPEC.loader
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)

RECIPE = ROOT / "packages" / "tuna-desktop" / "package.yaml"
SOURCE_SHA = "1" * 64
VENDOR_SHA = "2" * 64


@pytest.mark.parametrize(
    ("tag", "semver", "version"),
    [("v1.0.0-beta.1", "1.0.0-beta.1", "1.0.0~beta.1"), ("v1.0.0", "1.0.0", "1.0.0"), ("v1.2.3-rc.2", "1.2.3-rc.2", "1.2.3~rc.2")],
)
def test_tag_maps_to_reported_and_recipe_versions(tag: str, semver: str, version: str) -> None:
    assert prepare.parse_tag(tag) == (semver, version)


@pytest.mark.parametrize("tag", ["1.0.0", "v1.0", "v1.0.0-1", "v1.0.0_beta", "v1.0.0-beta-1"])
def test_tag_rejects_forms_the_recipe_cannot_carry(tag: str) -> None:
    with pytest.raises(SystemExit):
        prepare.parse_tag(tag)


def test_checked_in_recipe_agrees_on_one_tag() -> None:
    """version, TUNA_VERSION, smoke want=, source and vendor asset name one tag."""
    prepare.check_recipe(yaml.safe_load(RECIPE.read_text()))


def test_rewrite_fills_placeholders_for_the_tag() -> None:
    text = prepare.rewrite_recipe(RECIPE.read_text(), "v1.0.0-beta.1", SOURCE_SHA, VENDOR_SHA)
    recipe = yaml.safe_load(text)
    prepare.check_recipe(recipe)
    assert "PLACEHOLDER" not in text and "0" * 64 not in text
    assert recipe["version"] == "1.0.0~beta.1"
    assert recipe["source"] == {
        "url": "https://github.com/tuna-os/tuna-desktop/archive/refs/tags/v1.0.0-beta.1.tar.gz",
        "sha256": SOURCE_SHA,
        "directory": "tuna-desktop-1.0.0-beta.1",
    }
    assert recipe["sources"][0]["url"].endswith("/tuna-desktop-vendor-v1.0.0-beta.1/tuna-desktop-vendor.tar.gz")
    assert recipe["sources"][0]["sha256"] == VENDOR_SHA
    assert recipe["build"]["environment"]["TUNA_VERSION"] == "v1.0.0-beta.1"


def test_rewrite_moves_to_a_later_tag_and_resets_release() -> None:
    beta = prepare.rewrite_recipe(RECIPE.read_text(), "v1.0.0-beta.1", SOURCE_SHA, VENDOR_SHA)
    beta = beta.replace("\nrelease: 1\n", "\nrelease: 3\n")
    final = prepare.rewrite_recipe(beta, "v1.0.0", "3" * 64, "4" * 64)
    recipe = yaml.safe_load(final)
    prepare.check_recipe(recipe)
    assert recipe["version"] == "1.0.0"
    assert recipe["release"] == 1
    assert "want=1.0.0;" in recipe["verify"]["smoke"]
    # Only the tracked fields move; every other line is byte-identical.
    assert len(final.splitlines()) == len(beta.splitlines())


def test_rewrite_refuses_placeholder_hashes() -> None:
    with pytest.raises(SystemExit):
        prepare.rewrite_recipe(RECIPE.read_text(), "v1.0.0-beta.1", "0" * 64, VENDOR_SHA)


def test_check_recipe_rejects_a_smoke_that_expects_another_version() -> None:
    recipe = yaml.safe_load(RECIPE.read_text())
    want = recipe["build"]["environment"]["TUNA_VERSION"].removeprefix("v")
    recipe["verify"]["smoke"] = recipe["verify"]["smoke"].replace(f"want={want};", "want=0.1.0;")
    with pytest.raises(SystemExit):
        prepare.check_recipe(recipe)
