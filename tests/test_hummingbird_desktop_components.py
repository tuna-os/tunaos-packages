"""The base-image component audit (#228) is an invariant of the manifest.

quay.io/hummingbird-community/bootc-os:latest ships 262 packages and none of
the desktop submodules/applets the editions need.  `components:` in
manifests/hummingbird-desktops.yaml attributes each of those gaps to the
desktop edition that must install it, and validate-hummingbird-catalog.py
fails any commit that drops a component from its desktop's install_packages.
These tests pin both halves: the issue's full component list stays declared,
and the validator actually rejects a manifest that loses one.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "manifests" / "hummingbird-desktops.yaml"

spec = importlib.util.spec_from_file_location(
    "validate_hummingbird_catalog",
    ROOT / "scripts" / "validate-hummingbird-catalog.py",
)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)

# Every component named by the #228 base-image audit, exactly as listed there.
COMPONENTS_228 = {
    "NetworkManager-openvpn-gnome",
    "NetworkManager-openconnect-gnome",
    "NetworkManager-wwan",
    "NetworkManager-tui",
    "blueman",
    "brightnessctl",
    "playerctl",
    "pavucontrol",
    "gnome-keyring",
    "nautilus",
    "xdg-desktop-portal-gnome",
    "xdg-desktop-portal-gtk",
    "xdg-desktop-portal-kde",
    "SwayNotificationCenter",
    "waybar",
    "fuzzel",
    "gtkgreet",
    "cage",
}


def catalog() -> dict:
    return yaml.safe_load(CATALOG.read_text())


def declared_components() -> set[str]:
    return {
        component
        for names in catalog().get("components", {}).values()
        for component in names
    }


def test_every_228_component_is_declared_in_the_manifest() -> None:
    missing = sorted(COMPONENTS_228 - declared_components())
    assert not missing, (
        "components named by the #228 base-image audit are not declared in "
        f"manifests/hummingbird-desktops.yaml components:: {missing}"
    )


def test_components_reference_only_known_desktops() -> None:
    desktops = set(catalog().get("desktops", {}))
    unknown = sorted(set(catalog().get("components", {})) - desktops)
    assert not unknown, f"components references desktop(s) with no definition: {unknown}"


def test_every_component_is_in_its_desktops_install_list() -> None:
    data = catalog()
    for desktop, names in data.get("components", {}).items():
        install = set(data["desktops"][desktop].get("install_packages") or [])
        missing = sorted(set(names) - install)
        assert not missing, (
            f"{desktop}: component(s) {missing} are declared but never "
            "installed; a component outside install_packages cannot reach "
            "the image"
        )


def test_required_packages_stay_inside_install_packages() -> None:
    """greetd/gtkgreet/cage joined xfce's contract surface with this fix."""
    data = catalog()
    for desktop, definition in data.get("desktops", {}).items():
        install = set(definition.get("install_packages") or [])
        orphans = sorted(set(definition.get("required_packages") or []) - install)
        assert not orphans, f"{desktop}: required but never installed: {orphans}"


def write_catalog(tmp_path: Path, data: dict) -> Path:
    # The validator resolves `root` as the catalog's parent-of-parent and
    # checks `local:` sources against it, so mirror the repo layout.
    manifests = tmp_path / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "gnome-50").mkdir(parents=True, exist_ok=True)
    path = manifests / "catalog.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def minimal_catalog(**overrides) -> dict:
    data = {
        "schema": 1,
        "target": {
            "id": "test",
            "baseurl": "https://example.invalid/$arch/",
            "r2_path": "test/x86_64",
            "dist": ".fc99",
        },
        "desktops": {
            "gnome": {"required_packages": ["a"], "sources": [{"local": "src/gnome-50"}], "install_packages": ["a", "b"]},
        },
        "components": {"gnome": ["b"]},
    }
    data.update(overrides)
    return data


def run_validator(path: Path) -> None:
    previous = sys.argv
    sys.argv = ["validate-hummingbird-catalog.py", str(path)]
    try:
        validator.main()
    finally:
        sys.argv = previous


def test_validator_accepts_a_catalog_whose_components_are_installed(tmp_path) -> None:
    path = write_catalog(tmp_path, minimal_catalog())
    run_validator(path)  # must not raise


def test_validator_rejects_a_component_missing_from_install(tmp_path, capsys) -> None:
    path = write_catalog(
        tmp_path, minimal_catalog(components={"gnome": ["b", "never-installed"]})
    )
    with pytest.raises(SystemExit):
        run_validator(path)
    assert "never-installed" in capsys.readouterr().err


def test_validator_rejects_components_for_an_unknown_desktop(tmp_path, capsys) -> None:
    path = write_catalog(
        tmp_path, minimal_catalog(components={"gnome": ["b"], "ghost": ["x"]})
    )
    with pytest.raises(SystemExit):
        run_validator(path)
    assert "ghost" in capsys.readouterr().err


def test_validator_rejects_a_required_package_never_installed(tmp_path, capsys) -> None:
    path = write_catalog(
        tmp_path,
        minimal_catalog(
            desktops={
                "gnome": {
                    "required_packages": ["a", "c"],
                    "sources": [{"local": "src/gnome-50"}],
                    "install_packages": ["a", "b"],
                }
            }
        ),
    )
    with pytest.raises(SystemExit):
        run_validator(path)
    assert "never installed" in capsys.readouterr().err
