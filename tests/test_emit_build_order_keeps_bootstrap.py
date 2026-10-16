"""Regenerating the build order used to delete the bootstrap tiers.

#268 added four bootstrap tiers -- the PEP-517 backends Hummingbird does not
ship -- by hand, to a file whose own header says GENERATED, DO NOT HAND-EDIT.
The generator knew nothing about them, so the next regeneration would have
dropped all ten packages, and the first thing anyone would have seen is every
Python build failing for want of a backend.

They are declared in the catalog now, so the generator emits them rather than
overwriting them. This pins that, and pins the emitter to a single flat
sequence of layers rather than one order per desktop.
"""

import importlib.util
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def gap():
    spec = importlib.util.spec_from_file_location(
        "gap", REPO / "scripts" / "gap_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CATALOG = {
    "target": {"id": "hummingbird", "r2_path": "hummingbird/x"},
    # The engine no longer carries any family's tree layout; the roots
    # manifest declares it, so the fixture must too.
    "source_paths": ["src/hummingbird"],
    "distgit_prefix": "src/hummingbird",
    "bootstrap": [
        {"name": "bootstrap-00",
         "packages": [{"path": "src/hummingbird/python-flit-core",
                       "distgit": "python-flit-core"}]},
        {"name": "bootstrap-01",
         "packages": [{"path": "src/hummingbird/python-wheel",
                       "distgit": "python-wheel"}]},
    ],
}
REPORT = {
    "measured_at": "2026-08-09T00:00:00+00:00",
    "target_index": {"primary_sha256": "a" * 64},
    "reference_index": {"primary_sha256": "b" * 64},
    "desktops": {},
}


def emit(gap, tmp_path, tiers, cycles=(), catalog=None):
    out = tmp_path / "order.yml"
    gap.emit_build_order(out, catalog or CATALOG, [list(t) for t in tiers],
                         [list(c) for c in cycles], REPORT, REPO)
    return yaml.safe_load(out.read_text())


def tier_names(order):
    return [t["name"] for t in order["tiers"]]


def packages(tier):
    return [p.get("distgit") or p["path"].rsplit("/", 1)[-1] for p in tier["packages"]]


def test_declared_bootstrap_tiers_come_first_and_verbatim(gap, tmp_path):
    order = emit(gap, tmp_path, [["gtk4"], ["nautilus"]])
    assert tier_names(order)[:2] == ["bootstrap-00", "bootstrap-01"]
    assert packages(order["tiers"][0]) == ["python-flit-core"]
    assert packages(order["tiers"][1]) == ["python-wheel"]


def test_a_catalog_with_no_bootstrap_section_still_emits(gap, tmp_path):
    catalog = {**CATALOG}
    del catalog["bootstrap"]
    order = emit(gap, tmp_path, [["gtk4"]], catalog=catalog)
    assert tier_names(order) == ["layer-00"]


def test_layers_are_flat_and_numbered_not_named_after_desktops(gap, tmp_path):
    order = emit(gap, tmp_path, [["a"], ["b"], ["c"]])
    assert tier_names(order)[2:] == ["layer-00", "layer-01", "layer-02"]


def test_a_bootstrap_package_is_not_emitted_again_in_a_layer(gap, tmp_path):
    """It is already built by the time the layers start."""
    order = emit(gap, tmp_path, [["python-wheel", "gtk4"], ["nautilus"]])
    everywhere = [n for t in order["tiers"] for n in packages(t)]
    assert everywhere.count("python-wheel") == 1
    assert "gtk4" in everywhere


def test_an_emptied_layer_is_dropped_but_the_numbering_does_not_shift(gap, tmp_path):
    """Layer 0 here holds nothing the bootstrap tiers did not already build.

    It is dropped rather than emitted empty -- but the layer that follows stays
    layer-01, because the number is the index the measurement computed, not the
    position in the file. That is what lets the report and the manifest be
    checked against each other package by package.
    """
    order = emit(gap, tmp_path, [["python-flit-core"], ["gtk4"]])
    assert tier_names(order) == ["bootstrap-00", "bootstrap-01", "layer-01"]
    assert packages(order["tiers"][-1]) == ["gtk4"]


def test_cycle_members_are_marked_bootstrap_true(gap, tmp_path):
    order = emit(gap, tmp_path, [["glib2", "gobject-introspection"]],
                 cycles=[["glib2", "gobject-introspection"]])
    layer = order["tiers"][-1]
    assert all(p.get("bootstrap") is True for p in layer["packages"]), layer


def test_a_local_spec_fedora_lacks_builds_after_every_layer(gap, tmp_path):
    """tuna-desktop is not in Fedora, so no layer can carry it (#832).

    The measurement reports it under roots_absent_from_reference; its reviewed
    spec directory is emitted in `local-00`, after the layers that build its
    dependencies. A `local:` that is a tree of specs (src/xfce-wayland) is not
    one package and is not emitted, and a local spec a layer already builds
    is not emitted twice.
    """
    root = tmp_path / "repo"
    (root / "src/hummingbird/tuna-desktop").mkdir(parents=True)
    (root / "src/hummingbird/tuna-desktop/tuna-desktop.spec").write_text("")
    (root / "src/hummingbird/greetd").mkdir(parents=True)
    (root / "src/hummingbird/greetd/greetd.spec").write_text("")
    (root / "src/xfce-wayland/xfwl4").mkdir(parents=True)
    (root / "src/xfce-wayland/xfwl4/xfwl4.spec").write_text("")
    catalog = {
        **CATALOG,
        "desktops": {
            "tuna": {"sources": [{"local": "src/hummingbird/tuna-desktop"},
                                 {"local": "src/hummingbird/greetd"}]},
            "xfce": {"sources": [{"local": "src/xfce-wayland"}]},
            "kde": {"sources": [{"fedora_distgit": "plasma-desktop"}]},
        },
    }
    report = {**REPORT, "desktops": {"tuna": {}, "xfce": {}, "kde": {}}}
    out = tmp_path / "order.yml"
    gap.emit_build_order(out, catalog, [["gtk4-layer-shell"], ["greetd"]], [],
                         report, root)
    order = yaml.safe_load(out.read_text())
    assert tier_names(order)[-1] == "local-00"
    assert order["tiers"][-1]["packages"] == [{"path": "src/hummingbird/tuna-desktop"}]
    everywhere = [n for t in order["tiers"] for n in packages(t)]
    assert everywhere.count("greetd") == 1
    assert "xfce-wayland" not in everywhere


def test_no_local_spec_means_no_local_tier(gap, tmp_path):
    order = emit(gap, tmp_path, [["gtk4"]])
    assert "local-00" not in tier_names(order)
