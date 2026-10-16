"""Pin the dual-maintained hummingbird specs to their Tideforge recipes.

Eight packages exist twice while the native EL10 pipeline and Tideforge run
in parallel (docs/PACKAGE_FACTORY.md): a hand-written spec under
src/hummingbird/<p>/ and a recipe under packages/<p>/. Nothing kept the two
at the same upstream release, so a version bump could land on one side and
silently leave the other stale -- the exact single-source failure Tideforge
exists to remove. Until the legacy pipeline retires, this file makes the two
systems disagree loudly instead of drifting quietly.

Version and source URL only: dependency and packaging divergence between the
spec and the renderer is expected while both pipelines exist.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

DUAL_MAINTAINED = [
    "cosmic-comp",
    "cosmic-settings",
    "cosmic-session",
    "cosmic-greeter",
    "xdg-desktop-portal-cosmic",
    "greetd",
    "niri",
    "tuna-desktop",
]


def spec_text(package: str) -> str:
    return (ROOT / "src" / "hummingbird" / package / f"{package}.spec").read_text()


def recipe(package: str) -> dict:
    return yaml.safe_load((ROOT / "packages" / package / "package.yaml").read_text())


def spec_field(text: str, field: str) -> str:
    match = re.search(rf"^{field}:\s*(\S+)", text, re.MULTILINE)
    assert match, f"spec has no {field}: tag"
    return match.group(1)


def expand_source0(text: str, package: str, version: str) -> str:
    """Expand the macros Source0 actually uses in these specs."""
    macros = {
        "name": package,
        "version": version,
        "version_no_tilde": version.replace("~", ""),
    }
    url = re.search(r"^URL:\s*(\S+)", text, re.MULTILINE)
    if url:
        macros["url"] = url.group(1)
    for match in re.finditer(r"^%global\s+(\S+)\s+(\S+)", text, re.MULTILINE):
        macros.setdefault(match.group(1), match.group(2))
    source0 = spec_field(text, "Source0")
    return re.sub(
        r"%\{([^}]+)\}",
        lambda m: macros.get(m.group(1), m.group(0)),
        source0,
    )


@pytest.mark.parametrize("package", DUAL_MAINTAINED)
def test_spec_version_matches_recipe(package: str) -> None:
    spec_version = spec_field(spec_text(package), "Version")
    recipe_version = str(recipe(package)["version"])
    assert spec_version == recipe_version, (
        f"{package}: src/hummingbird spec is at {spec_version} but "
        f"packages/{package} is at {recipe_version}. Bump both sides in the "
        "same change (docs/PACKAGE_FACTORY.md runs the pipelines in parallel; "
        "they must track the same upstream release)."
    )


@pytest.mark.parametrize("package", DUAL_MAINTAINED)
def test_spec_source_matches_recipe_upstream(package: str) -> None:
    """Same upstream repository and same release ref, not just same number."""
    data = recipe(package)
    recipe_url = data["source"]["url"]
    assert "/archive/" in recipe_url, f"{package}: recipe source is not an archive URL"
    repo_base = recipe_url.split("/archive/")[0]
    source0 = expand_source0(spec_text(package), package, str(data["version"]))
    assert source0.startswith(repo_base + "/archive/"), (
        f"{package}: spec Source0 ({source0}) does not fetch from the recipe's "
        f"upstream repository ({repo_base})."
    )
    # A prerelease's Version writes the tag's `-` as `~` (tuna-desktop
    # 1.0.0~beta.1 is tag v1.0.0-beta.1); the URL carries the tag form.
    upstream = str(data["version"]).replace("~", "-")
    assert upstream in source0, (
        f"{package}: spec Source0 ({source0}) does not reference upstream "
        f"release {upstream}."
    )


def test_the_dual_maintained_list_is_current() -> None:
    """Every package that exists on both sides must be pinned.

    A new src/hummingbird directory that shadows a recipe (or vice versa)
    joins the sync check by construction, not by someone remembering.
    """
    hummingbird = {
        path.parent.name
        for path in (ROOT / "src" / "hummingbird").glob("*/*.spec")
        if path.stem == path.parent.name
    }
    recipes = {path.parent.name for path in (ROOT / "packages").glob("*/package.yaml")}
    both = sorted(hummingbird & recipes)
    assert both == sorted(DUAL_MAINTAINED), (
        f"dual-maintained packages changed: {both}; update DUAL_MAINTAINED "
        "so the sync tests cover exactly the packages that exist twice."
    )
