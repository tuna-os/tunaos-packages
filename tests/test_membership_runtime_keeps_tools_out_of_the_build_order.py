"""`membership: runtime` builds what images ship; tools stay in the buildroot.

The fold fix (#289) revealed that the true self-hosting closure of the five
desktops is 3282 source packages against a runtime closure of 663 -- a 5x
factory spent on bison, transfig, gtk-doc and the Java stack the documentation
toolchains drag in, none of which any image ships.  The committed 1248-package
order was neither closure: it was whatever the buggy fold happened to reach.

The decision (2026-08-17): the build order contains the runtime closure only,
and BuildRequires-only tools come from the buildroot's inherited Rawhide
fallback at priority 99.  Membership and ordering stay separate questions --
ordering always uses the real BuildRequires: graph, so members that
build-depend on each other still come up in the right tiers.
"""
from __future__ import annotations

import importlib.util
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "gap", ROOT / "scripts" / "gap_engine.py"
)
gap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gap)


def reference():
    """app -> lib at runtime; lib's SOURCE BuildRequires a build tool."""
    return {
        "packages": {
            "app": {"arch": "x86_64", "evr": "1-1", "srpm": "app-1-1.fc45.src.rpm",
                    "requires": ["lib"]},
            "lib": {"arch": "x86_64", "evr": "1-1", "srpm": "lib-1-1.fc45.src.rpm",
                    "requires": []},
            "buildtool": {"arch": "x86_64", "evr": "1-1",
                          "srpm": "buildtool-1-1.fc45.src.rpm", "requires": []},
        },
        "provides": {"app": {"app"}, "lib": {"lib"}, "buildtool": {"buildtool"}},
        "files": set(),
    }


SOURCE_INDEX = {"app": ["lib"], "lib": ["buildtool"], "buildtool": []}


def test_runtime_membership_excludes_the_build_tool() -> None:
    """closure() without a source index is the runtime membership walk."""
    seen, _, _ = gap.closure(["app"], reference(), have=set(), source_index=None)
    assert seen == {"app", "lib"}


def test_ordering_still_uses_buildrequires_between_members() -> None:
    """lib must tier before app: app's source BuildRequires lib."""
    tiers, cycles = gap.tier_sources(
        ["app", "lib"], reference(), have=set(), source_index=SOURCE_INDEX
    )
    assert tiers == [["lib"], ["app"]]
    assert cycles == []


def test_retained_package_walks_its_runtime_closure_without_selfhosting() -> None:
    """A served repair root needs its runtime library, not unrelated tools."""
    measured, need = gap.measure_roots(
        ["app"], reference(), set(), {}, SOURCE_INDEX, "runtime"
    )

    assert need == {"app", "lib"}
    assert measured["binary_packages_to_build"] == ["app", "lib"]
    assert measured["source_packages_to_build"] == ["app", "lib"]
    assert "buildtool" not in measured["source_packages_to_build"]


def test_catalog_declares_runtime_membership() -> None:
    """The policy is in the catalog so every regeneration honours it."""
    catalog = yaml.safe_load(
        (ROOT / "manifests" / "hummingbird-desktops.yaml").read_text()
    )
    assert catalog.get("membership") == "runtime"


def test_committed_build_order_is_the_runtime_closure() -> None:
    """The generated file records its membership; a selfhost regeneration
    (or one from the pre-#289 fold bug) would not carry this marker."""
    text = (ROOT / "build-order-hummingbird-desktops.yml").read_text()
    assert "Membership is `runtime`" in text


def test_report_records_membership() -> None:
    import json
    report = json.loads(
        (ROOT / "docs" / "hummingbird-desktop-gap.json").read_text()
    )
    assert report["membership"] == "runtime"
