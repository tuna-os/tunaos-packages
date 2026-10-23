"""A package in another family's manifest does not complete native GNOME 51."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def native_paths(document):
    return {
        package["path"]
        for tier in document["tiers"]
        for package in tier["packages"]
    }


def assert_native_coverage(document):
    expected = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src/gnome-51").iterdir()
        if path.is_dir() and list(path.glob("*.spec"))
    }
    missing = expected - native_paths(document)
    assert not missing, f"GNOME 51 native family omits: {sorted(missing)}"


def test_every_gnome51_source_is_in_its_native_family():
    assert_native_coverage(yaml.safe_load((ROOT / "build-order-gnome51.yml").read_text()))


@pytest.mark.parametrize("package", ["gnome-online-accounts", "orca"])
def test_other_family_coverage_cannot_hide_native_omission(package):
    document = yaml.safe_load((ROOT / "build-order-gnome51.yml").read_text())
    for tier in document["tiers"]:
        tier["packages"] = [
            entry for entry in tier["packages"]
            if entry["path"] != f"src/gnome-51/{package}"
        ]
    with pytest.raises(AssertionError, match=package):
        assert_native_coverage(document)


def test_goa_follows_libadwaita_and_precedes_control_center():
    document = yaml.safe_load((ROOT / "build-order-gnome51.yml").read_text())
    tiers = {
        entry["path"]: index
        for index, tier in enumerate(document["tiers"])
        for entry in tier["packages"]
    }
    assert tiers["src/gnome-51/libadwaita"] < tiers["src/gnome-51/gnome-online-accounts"]
    assert tiers["src/gnome-51/gnome-online-accounts"] < tiers["src/gnome-51/gnome-control-center"]


def test_native_queue_defaults_select_51_and_keep_gdm_compatibility():
    queues = yaml.safe_load((ROOT / "manifests/target-queues/gnome.yaml").read_text())["queues"]
    for target in ("el10", "alma10", "alma10-kitten"):
        assert queues[target]["build_order"] == "build-order-gnome51.yml"
        assert queues[target]["compatibility_overlay"] == "src/deps/gnome50-el10-compat"


def test_native_family_does_not_build_the_same_spec_twice():
    document = yaml.safe_load((ROOT / "build-order-gnome51.yml").read_text())
    # Bootstrap and full specs may share a package path, but not the same spec.
    builds = [
        (entry["path"], entry.get("spec_override"))
        for tier in document["tiers"]
        for entry in tier["packages"]
    ]
    assert len(builds) == len(set(builds)), "duplicate native package/spec build"
