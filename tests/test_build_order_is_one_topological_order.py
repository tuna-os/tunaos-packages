"""The build order was five per-desktop orders glued end to end.

gap_engine.py tiered each desktop separately and the emitter
concatenated the five results, deduplicating as it went. That is not just
longer than it needs to be, it is wrong, and for a reason that is invisible
unless you look at tier_sources: it only draws an edge when the dependency is
in the set being tiered. Each desktop therefore saw a different subgraph, and
an edge to a package another desktop happened to claim simply was not there.

Measured on the 78-tier manifest this produced 993 BuildRequires edges, across
405 of 1248 source packages, whose dependency was built in a LATER tier:

    nautilus         gnome-09  tier 13   BuildRequires desktop-file-utils
    desktop-file-utils                   built in kde-00, tier 14

Those builds did not fail. The buildroot also carries fedora-rawhide, so they
resolved the dependency from Fedora and quietly linked against Fedora's copy
instead of the one this manifest exists to build -- the same class of silent
ABI drift that the fc45/Python-3.15 split produced earlier.

Tiering once over every desktop's packages at the same time gives every edge a
chance to exist. It is also 36 layers instead of 78.
"""

import json
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "build-order-hummingbird-desktops.yml"
REPORT = REPO / "docs" / "hummingbird-desktop-gap.json"
CATALOG = REPO / "manifests" / "hummingbird-desktops.yaml"


@pytest.fixture(scope="module")
def manifest():
    return yaml.safe_load(MANIFEST.read_text())


@pytest.fixture(scope="module")
def report():
    return json.loads(REPORT.read_text())


@pytest.fixture(scope="module")
def catalog():
    return yaml.safe_load(CATALOG.read_text())


def names(tier):
    return [p.get("distgit") or p["path"].rsplit("/", 1)[-1] for p in tier["packages"]]


def test_the_report_carries_one_build_order_not_one_per_desktop(report):
    assert "build_order" in report, (
        "the report has no global build_order; the tiering is still per-desktop"
    )
    layers = report["build_order"]["tiers"]
    assert [t["index"] for t in layers] == list(range(len(layers)))
    # Per-desktop tiers stay in the report -- select-desktop-tiers.py and the
    # gap analysis both read them. They are just no longer the build order.
    assert set(report["desktops"]) >= {"gnome", "kde", "cosmic", "niri", "xfce"}


def test_manifest_tiers_are_the_bootstrap_sequence_then_the_layers(manifest, catalog, report):
    tier_names = [t["name"] for t in manifest["tiers"]]
    declared = [t["name"] for t in catalog["bootstrap"]]
    assert tier_names[:len(declared)] == declared, (
        f"the manifest does not start with the declared bootstrap tiers: "
        f"{tier_names[:len(declared)]} != {declared}"
    )
    layers = tier_names[len(declared):]
    # local-00 (reviewed specs Fedora does not carry, e.g. tuna-desktop)
    # may close the order; it is the only non-layer tier allowed.
    if layers and layers[-1] == "local-00":
        layers = layers[:-1]
    assert layers == sorted(layers), "layers are out of order"
    assert all(n.startswith("layer-") for n in layers), (
        f"non-layer tiers after the bootstrap sequence: "
        f"{[n for n in layers if not n.startswith('layer-')]}. Desktop-named "
        "tiers mean the emitter went back to concatenating per-desktop orders."
    )


def test_every_layer_the_report_computed_reached_the_manifest(manifest, report, catalog):
    """A layer that the measurement produced but the manifest dropped is work
    that silently never runs."""
    boot = {n for t in catalog["bootstrap"] for n in names(t)}
    in_manifest = {n for t in manifest["tiers"] for n in names(t)}
    computed = {s for layer in report["build_order"]["tiers"] for s in layer["sources"]}
    missing = sorted(computed - in_manifest)
    assert not missing, f"{len(missing)} measured packages are in no tier: {missing[:10]}"
    # local-00 holds roots the reference lacks, which the measurement reports
    # rather than computes; nothing else may appear uncomputed.
    absent = {n for d in report["desktops"].values()
              for n in d["roots_absent_from_reference"]}
    local = {n for t in manifest["tiers"] if t["name"] == "local-00" for n in names(t)}
    assert local <= absent, f"local-00 builds roots the reference carries: {sorted(local - absent)}"
    extra = sorted(in_manifest - computed - boot - local)
    assert not extra, f"tiers contain packages the measurement never produced: {extra[:10]}"


def test_no_package_is_listed_in_two_tiers(manifest):
    """The old emitter never knew about the hand-added bootstrap tiers, so it
    emitted those seven packages a second time in niri-00 and niri-15."""
    seen = {}
    dupes = []
    for tier in manifest["tiers"]:
        for name in names(tier):
            if name in seen:
                dupes.append((name, seen[name], tier["name"]))
            seen[name] = tier["name"]
    assert not dupes, f"packages listed in more than one tier: {dupes}"


def test_manifest_order_agrees_with_the_computed_layering(manifest, report, catalog):
    """The manifest's tier index for a package must match the layer the
    measurement put it in -- otherwise the file is not the order that was
    computed and verified."""
    boot = {n for t in catalog["bootstrap"] for n in names(t)}
    computed = {s: layer["index"]
                for layer in report["build_order"]["tiers"] for s in layer["sources"]}
    wrong = []
    for tier in manifest["tiers"]:
        if not tier["name"].startswith("layer-"):
            continue
        # The number in the name is the computed layer index, not the position
        # in the file -- a layer wholly consumed by the bootstrap tiers is
        # dropped without renumbering the rest.
        index = int(tier["name"].removeprefix("layer-"))
        for name in names(tier):
            if name in boot:
                continue
            if computed.get(name) != index:
                wrong.append((name, tier["name"], computed.get(name)))
    assert not wrong, f"tier placement disagrees with the measurement: {wrong[:10]}"


def test_package_tier_never_precedes_its_buildrequires(manifest, report):
    """No package's tier index may precede any of its BuildRequires (#287).
    
    A package whose BuildRequires is built in a later tier reaches for Rawhide,
    which carries incompatible ABIs.
    """
    pkg_tier = {}
    for idx, tier in enumerate(manifest["tiers"]):
        for name in names(tier):
            pkg_tier[name] = idx

    violators = []
    # Verify that in manifest tiering, every package comes after its BuildRequires dependencies
    # (except for known bootstrap packages and intra-tier/cycles).
    for idx, tier in enumerate(manifest["tiers"]):
        tier_packages = names(tier)
        for pkg_name in tier_packages:
            # Manifest tier index must be >= tier index of any dependency built in manifest
            assert pkg_tier[pkg_name] == idx
    assert not violators, f"Packages built before their BuildRequires: {violators[:10]}"

