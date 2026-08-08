"""The manifest and the gap report must come from the same measurement.

scripts/measure-hummingbird-gap.py writes both files in one run:
build-order-hummingbird-desktops.yml (with the measurement stamped into its
header) and docs/hummingbird-desktop-gap.json. Nothing made anyone commit
them together, and on main they drifted two days apart:

    manifest  # Measured 2026-08-08T14:44:50   target primary sha256 3c2eaf99...
    report      measured_at 2026-08-06T14:49:23  target primary sha256 b92541ea...

1403 source packages in the report's tiers against 1255 in the manifest.

That is not a cosmetic mismatch. scripts/select-desktop-tiers.py resolves a
desktop to tiers by intersecting the report's source_packages_to_build with
the manifest's tier contents, so a stale report silently selects the wrong
tiers -- and its coverage assertion still passes, because it checks the two
files against each other rather than against reality.

It also cost a live debugging session: `vala` is absent from every desktop in
the stale report while sitting in cosmic-10 of the manifest, which reads
exactly like a closure bug and is not one.
"""

import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "build-order-hummingbird-desktops.yml"
REPORT = REPO / "docs" / "hummingbird-desktop-gap.json"


def manifest_header() -> dict:
    text = MANIFEST.read_text()
    out = {}
    for key, pattern in (
        ("measured_at", r"^# Measured (\S+)"),
        ("target", r"^# target primary\.xml\s+sha256 ([0-9a-f]{64})"),
        ("reference", r"^# reference primary\.xml sha256 ([0-9a-f]{64})"),
    ):
        m = re.search(pattern, text, re.M)
        assert m, f"manifest header has no {key}; the generator stamps it"
        out[key] = m.group(1)
    return out


def report() -> dict:
    return json.loads(REPORT.read_text())


def test_same_measurement_timestamp():
    assert manifest_header()["measured_at"] == report()["measured_at"], (
        "the manifest and the gap report were produced by different runs of "
        "measure-hummingbird-gap.py; regenerate both together"
    )


def test_same_target_index():
    assert manifest_header()["target"] == report()["target_index"]["primary_sha256"], (
        "the manifest was measured against a different target primary.xml "
        "than the report describes"
    )


def test_same_reference_index():
    assert manifest_header()["reference"] == report()["reference_index"]["primary_sha256"]


def test_every_manifest_package_is_in_the_report():
    """The strongest of the three: content, not just provenance."""
    manifest = yaml.safe_load(MANIFEST.read_text())
    in_manifest = {
        p.get("distgit") or p["path"].rsplit("/", 1)[-1]
        for tier in manifest["tiers"]
        for p in tier["packages"]
    }
    r = report()
    in_report = {
        name
        for desktop in r["desktops"].values()
        for tier in desktop["tiers"]
        for name in tier["sources"]
    }
    bootstrap = {
        p.get("distgit") or p["path"].rsplit("/", 1)[-1]
        for tier in manifest["tiers"]
        if tier["name"].startswith("bootstrap-")
        for p in tier["packages"]
    }
    orphans = sorted(in_manifest - in_report - bootstrap)
    assert not orphans, (
        f"{len(orphans)} package(s) are in the manifest but in no tier of the "
        f"report, so the two disagree about what is being built: "
        f"{', '.join(orphans[:15])}"
        + (" ..." if len(orphans) > 15 else "")
    )
