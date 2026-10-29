"""The gap closure must walk BuildRequires:, not only runtime Requires:.

The target is a base OS image, so by definition it ships no build-only
packages.  A build tool appears only in BuildRequires: and never in any runtime
dependency, so a closure over Requires: alone cannot see one -- it is not that
the tool is judged present, it is that it is never considered.

Measured against Hummingbird's own index (22335 capabilities) and Fedora's
source index (23166 SRPMs): 413 capabilities the build order needs that nothing
provides, blocking 462 of its 680 packages.  extra-cmake-modules alone blocks
112, kf6-rpm-macros 106, gtk-doc 62.  bison and flex -- 17 each -- are the
tell: ordinary buildroot tools, absent from a base OS precisely because nothing
at runtime needs them.

The PEP-517 backends fixed in #268/#269 were the first instance to reach CI.
They were one case of this class, not a special case, which is why the fix
belongs in the closure rather than in a hand-written bootstrap tier.
"""
from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "gap", ROOT / "scripts" / "gap_engine.py"
)
gap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gap)


def reference():
    """app -> lib at runtime; the SOURCE of lib BuildRequires a build tool."""
    return {
        "packages": {
            "app": {"arch": "x86_64", "evr": "1-1", "srpm": "app-1-1.fc45.src.rpm",
                    "requires": ["lib"]},
            "lib": {"arch": "x86_64", "evr": "1-1", "srpm": "lib-1-1.fc45.src.rpm",
                    "requires": []},
            "buildtool": {"arch": "x86_64", "evr": "1-1",
                          "srpm": "buildtool-1-1.fc45.src.rpm", "requires": ["helper"]},
            "helper": {"arch": "noarch", "evr": "1-1",
                       "srpm": "helper-1-1.fc45.src.rpm", "requires": []},
        },
        "provides": {
            "app": {"app"}, "lib": {"lib"},
            "buildtool": {"buildtool"}, "helper": {"helper"},
        },
        "files": set(),
    }


SOURCE_INDEX = {"app": [], "lib": ["buildtool"], "buildtool": [], "helper": []}


def test_runtime_only_closure_cannot_see_a_build_tool() -> None:
    """The old behaviour, pinned so the regression is legible."""
    seen, _, _ = gap.closure(["app"], reference(), have=set())
    assert seen == {"app", "lib"}
    assert "buildtool" not in seen


def test_buildrequires_are_pulled_into_the_closure() -> None:
    seen, _, _ = gap.closure(["app"], reference(), set(), source_index=SOURCE_INDEX)
    assert "buildtool" in seen, (
        "lib's source BuildRequires buildtool and the target does not ship it, "
        "so it has to be built"
    )


def test_the_build_tools_own_runtime_deps_come_too() -> None:
    """A tool that cannot run is no better than one that is absent."""
    seen, _, _ = gap.closure(["app"], reference(), set(), source_index=SOURCE_INDEX)
    assert "helper" in seen, (
        "buildtool Requires: helper -- pulling the tool in without its runtime "
        "closure yields an uninstallable buildroot"
    )


def test_what_the_target_already_ships_is_not_rebuilt() -> None:
    seen, _, _ = gap.closure(
        ["app"], reference(), have={"buildtool"}, source_index=SOURCE_INDEX
    )
    assert "buildtool" not in seen
    assert "helper" not in seen


def test_a_buildrequires_nothing_provides_is_reported_not_dropped() -> None:
    """Silence here is how 413 missing capabilities went unnoticed."""
    src = dict(SOURCE_INDEX, lib=["nonexistent-tool"])
    _, _, unresolved = gap.closure(["app"], reference(), set(), source_index=src)
    assert "nonexistent-tool" in unresolved
    assert "lib" in unresolved["nonexistent-tool"]


def test_buildrequires_found_even_when_every_runtime_dep_is_already_shipped() -> None:
    """The KDE case: plasma-workspace's runtime deps are in the target, so the
    runtime walk stops immediately -- but extra-cmake-modules must still be
    reached through the BuildRequires fold.

    Without the fixpoint fold (#289) this would return {plasma-workspace} only.
    """
    ref = {
        "packages": {
            "plasma-workspace": {
                "arch": "x86_64", "evr": "1-1",
                "srpm": "plasma-workspace-1-1.fc45.src.rpm",
                "requires": ["kf6-kio", "qt6-qtbase"],
            },
            "extra-cmake-modules": {
                "arch": "noarch", "evr": "1-1",
                "srpm": "extra-cmake-modules-1-1.fc45.src.rpm",
                "requires": ["cmake"],
            },
            "cmake": {
                "arch": "x86_64", "evr": "1-1",
                "srpm": "cmake-1-1.fc45.src.rpm",
                "requires": [],
            },
        },
        "provides": {
            "plasma-workspace": {"plasma-workspace"},
            "kf6-kio": {"kf6-kio"},
            "qt6-qtbase": {"qt6-qtbase"},
            "extra-cmake-modules": {"extra-cmake-modules"},
            "cmake": {"cmake"},
        },
        "files": set(),
    }
    source_index = {
        "plasma-workspace": ["extra-cmake-modules"],
        "extra-cmake-modules": [],
        "cmake": [],
    }
    # All of plasma-workspace's runtime Requires: are already shipped.
    have = {"kf6-kio", "qt6-qtbase"}
    seen, _, _ = gap.closure(["plasma-workspace"], ref, have, source_index)
    assert "extra-cmake-modules" in seen, (
        "BuildRequires-only package not reached when runtime closure is trivial"
    )
    assert "cmake" in seen, (
        "runtime dep of the build tool must also be reached"
    )


def versioned_reference() -> dict:
    return {
        "packages": {
            "app": {
                "arch": "x86_64",
                "evr": "1-1",
                "srpm": "app-1-1.fc45.src.rpm",
                "requires": ["lib"],
                "requires_versioned": [("lib", ">=", "0:2.0-1")],
            },
            "lib": {
                "arch": "x86_64",
                "evr": "3.0-1",
                "srpm": "lib-3.0-1.fc45.src.rpm",
                "requires": [],
                "requires_versioned": [],
            },
        },
        "provides": {"app": {"app"}, "lib": {"lib"}},
        "provides_evr": {
            "app": {"1-1"},
            "lib": {"3.0-1"},
        },
        "files": set(),
    }


def test_an_older_target_provider_does_not_satisfy_a_versioned_requirement() -> None:
    seen, _, unresolved = gap.closure(
        ["app"],
        versioned_reference(),
        {"lib"},
        have_evr={"lib": {"1.0-1"}},
    )
    assert "lib" in seen
    assert unresolved == {}


def test_a_target_provider_at_the_required_version_stays_out_of_the_closure() -> None:
    seen, _, unresolved = gap.closure(
        ["app"],
        versioned_reference(),
        {"lib"},
        have_evr={"lib": {"2.0-1"}},
    )
    assert "lib" not in seen
    assert unresolved == {}


def test_versioned_buildrequires_order_a_rebuilt_provider_before_its_consumer() -> None:
    tiers, cycles = gap.tier_sources(
        ["app", "lib"],
        versioned_reference(),
        {"lib"},
        {"app": ["lib"]},
        have_evr={"lib": {"1.0-1"}},
        source_versioned_index={"app": [("lib", ">=", "0:2.0-1")]},
    )
    assert tiers == [["lib"], ["app"]]
    assert cycles == []
