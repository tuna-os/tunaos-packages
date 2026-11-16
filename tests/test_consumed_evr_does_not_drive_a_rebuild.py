"""A consumed repository's EVR never selects a package for rebuild.

The engine compared every EVR a name carries -- the target's and the consumed
repository's alike -- against Rawhide.  Hummingbird consumes GNOME 51 from
utah-packages, which ships 51~beta-1 while Rawhide ships 51.1.  Every GNOME
root therefore read as "older than the reference" and joined the Hummingbird
build order, although consumed packages are never rebuilt here and the
validator makes `consumed_from` and `sources` exclusive.

Version-driven rebuilds now read the target's own index only.  Consumed
repositories still count as HAD (presence and satisfaction of versioned
requirements), so nothing they ship is ever a gap.

These tests drive gap_engine.main() end to end, with the registry and index
fetches replaced by in-memory primaries.  The consumed index goes through the
real consumed_indexes() path, so the EVRs main() folds in are the ones
production folds in.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "gap", ROOT / "scripts" / "gap_engine.py"
)
gap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gap)

TARGET_URL = "https://target.example/hummingbird/x86_64/"
REFERENCE_URL = "https://reference.example/fedora-rawhide/x86_64/"
UTAH_URL = "oci://ghcr.io/projectbluefin/utah-packages@sha256:" + "9c" * 32

GNOME = ("gnome-shell", "gdm", "mutter", "gnome-session")


def primary_xml(*packages: dict) -> bytes:
    """A primary.xml with the given packages.

    Each package is {name, ver, rel, requires}; a requires item is a bare
    capability name or (name, flags, ver, rel) for a versioned one.
    """
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<metadata xmlns="http://linux.duke.edu/metadata/common" '
        'xmlns:rpm="http://linux.duke.edu/metadata/rpm">',
    ]
    for pkg in packages:
        requires = []
        for item in pkg.get("requires", ()):
            if isinstance(item, str):
                requires.append(f'<rpm:entry name="{item}"/>')
            else:
                name, flags, ver, rel = item
                requires.append(
                    f'<rpm:entry name="{name}" flags="{flags}" epoch="0" '
                    f'ver="{ver}" rel="{rel}"/>'
                )
        out.append(
            f'<package type="rpm"><name>{pkg["name"]}</name>'
            f'<arch>x86_64</arch>'
            f'<version epoch="0" ver="{pkg["ver"]}" rel="{pkg["rel"]}"/>'
            f'<format><rpm:requires>{"".join(requires)}</rpm:requires>'
            f'</format></package>'
        )
    out.append("</metadata>")
    return "\n".join(out).encode()


# Fedora Rawhide: GNOME 51.1, and a desktop root that needs a newer lib.
REFERENCE_XML = primary_xml(
    {"name": "gnome-shell", "ver": "51.1", "rel": "1.fc45", "requires": ["mutter"]},
    {"name": "mutter", "ver": "51.1", "rel": "1.fc45", "requires": []},
    {"name": "gdm", "ver": "51.1", "rel": "1.fc45", "requires": ["gnome-session"]},
    {"name": "gnome-session", "ver": "51.1", "rel": "1.fc45", "requires": []},
    {"name": "app", "ver": "1.0", "rel": "1.fc45",
     "requires": [("lib", "GE", "2.0", "1")]},
    {"name": "lib", "ver": "3.0", "rel": "1.fc45", "requires": []},
)

# Hummingbird's own repository: no GNOME at all, an older lib.
TARGET_XML = primary_xml(
    {"name": "glibc", "ver": "2.42", "rel": "1.hum1", "requires": []},
    {"name": "lib", "ver": "1.0", "rel": "1.hum1", "requires": []},
)

# utah-packages, enabled next to the target: GNOME 51~beta-1, the EVR the
# consumer image actually runs.
UTAH_XML = primary_xml(
    {"name": "gnome-shell", "ver": "51~beta", "rel": "1.hum1", "requires": ["mutter"]},
    {"name": "mutter", "ver": "51~beta", "rel": "1.hum1", "requires": []},
    {"name": "gdm", "ver": "51~beta", "rel": "1.hum1", "requires": ["gnome-session"]},
    {"name": "gnome-session", "ver": "51~beta", "rel": "1.hum1", "requires": []},
)

FACTORY = {
    "targets": {
        "hummingbird": {
            "repository": "rpm-md",
            "gap_measurement": {
                "roots_manifest": "manifests/hummingbird-desktops.yaml",
                "target_index": TARGET_URL,
                "reference_index": REFERENCE_URL,
                "consumed_indexes": [
                    {"id": "utah-packages", "index": UTAH_URL, "serves": ["gnome"]},
                ],
            },
        },
    },
}

CATALOG = {
    "schema": 1,
    "target": {
        "id": "hummingbird-test",
        "baseurl": TARGET_URL,
        "r2_path": "hummingbird/test",
        "dist": ".bfin1",
    },
    "membership": "runtime",
    "desktops": {
        "gnome": {
            "consumed_from": "utah-packages",
            "required_packages": ["gnome-shell", "gdm"],
            "install_packages": [],
        },
        "versioned": {
            "required_packages": ["app"],
            "install_packages": [],
        },
    },
}


def run_measure(tmp_path, monkeypatch) -> dict:
    """Run gap_engine.main() over the fixtures and return its JSON report."""
    blobs = {
        TARGET_URL: TARGET_XML,
        REFERENCE_URL: REFERENCE_XML,
        UTAH_URL: UTAH_XML,
    }

    def fake_primary_of(url, cache, arch="x86_64"):
        return blobs[url], {"baseurl": url, "revision": "test"}

    monkeypatch.setattr(gap, "primary_of", fake_primary_of)
    factory = tmp_path / "package-factory.yaml"
    factory.write_text(yaml.safe_dump(FACTORY))
    catalog = tmp_path / "hummingbird-desktops.yaml"
    catalog.write_text(yaml.safe_dump(CATALOG))
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", [
        "gap_engine.py",
        "--target", "hummingbird",
        "--factory", str(factory),
        "--catalog", str(catalog),
        "--cache", str(tmp_path / "cache"),
        "--report-json", str(report_path),
    ])
    gap.main()
    return json.loads(report_path.read_text())


def test_utah_gnome_at_an_older_evr_is_not_rebuilt_against_rawhide(
    tmp_path, monkeypatch
) -> None:
    """The Utah/Rawhide case: 51~beta-1 consumed, 51.1 in Rawhide.

    Before the fix all four GNOME packages landed in the build order.
    """
    report = run_measure(tmp_path, monkeypatch)
    gnome = report["desktops"]["gnome"]

    assert sorted(gnome["roots_missing_from_target"]) == ["gdm", "gnome-shell"], (
        "the roots are still declared and still reported as not in the target"
    )
    rebuilt = set(gnome["binary_packages_to_build"]) & set(GNOME)
    assert rebuilt == set(), (
        f"consumed GNOME packages must never be rebuilt here, got {sorted(rebuilt)}"
    )
    assert not set(gnome["source_packages_to_build"]) & set(GNOME)
    assert all(
        not set(tier["sources"]) & set(GNOME)
        for tier in report["build_order"]["tiers"]
    ), "no consumed GNOME package may appear anywhere in the build order"


def test_a_target_package_older_than_rawhide_is_still_rebuilt(
    tmp_path, monkeypatch
) -> None:
    """Guard: excluding consumed EVRs must not stop version-driven rebuilds
    of the target's own packages.

    `app` needs lib >= 2.0; the target ships lib 1.0, so lib is rebuilt from
    Rawhide's 3.0, and app (absent from the target) is built too.
    """
    report = run_measure(tmp_path, monkeypatch)
    versioned = report["desktops"]["versioned"]
    assert "lib" in versioned["binary_packages_to_build"], (
        "the target's stale lib cannot satisfy app's versioned requirement"
    )
    assert "app" in versioned["binary_packages_to_build"]

