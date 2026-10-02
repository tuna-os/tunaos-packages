#!/usr/bin/env python3
"""Validate Hummingbird's project-owned desktop RPM source catalog."""
from __future__ import annotations

import argparse
import pathlib
import sys

import yaml


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", type=pathlib.Path)
    parser.add_argument("--factory", type=pathlib.Path,
                        help="package-factory.yaml whose target declares the consumed_indexes "
                             "a desktop may name in consumed_from (default: manifests/package-factory.yaml "
                             "next to the catalog)")
    parser.add_argument("--target", default="hummingbird")
    args = parser.parse_args()
    root = args.catalog.resolve().parents[1]
    data = yaml.safe_load(args.catalog.read_text())
    if data.get("schema") != 1:
        fail("schema must be 1")
    for field in ("id", "baseurl", "r2_path", "dist"):
        if not data.get("target", {}).get(field):
            fail(f"target.{field} is required")
    # A desktop is either BUILT here (sources) or CONSUMED from a repository
    # the target contract declares (consumed_from -> gap_measurement.
    # consumed_indexes[].id), never both and never neither. GNOME on
    # hummingbird is consumed from utah-packages (#629).
    factory_path = args.factory or (root / "manifests" / "package-factory.yaml")
    consumable: set[str] = set()
    if factory_path.exists():
        factory = yaml.safe_load(factory_path.read_text())
        measurement = ((factory.get("targets") or {}).get(args.target) or {}).get("gap_measurement") or {}
        consumable = {entry["id"] for entry in measurement.get("consumed_indexes") or []}
    for desktop, definition in data.get("desktops", {}).items():
        if not definition.get("required_packages"):
            fail(f"{desktop} needs required_packages")
        consumed = definition.get("consumed_from")
        if consumed:
            if definition.get("sources"):
                fail(f"{desktop}: consumed_from and sources are exclusive -- a desktop is built here or consumed, not both")
            if consumed not in consumable:
                fail(f"{desktop}: consumed_from {consumed!r} is not a consumed_indexes id on the "
                     f"{args.target} target in {factory_path} (have: {sorted(consumable) or 'none'})")
            definition = {**definition, "sources": []}
        elif not definition.get("sources"):
            fail(f"{desktop} needs packages and sources (or consumed_from)")
        # install_packages is what tunaOS actually installs; required_packages
        # is only the contract surface its desktop gate checks. The gap
        # measurement resolves both, and a required package missing from the
        # install list is a package nothing would install — the exact shape of
        # the GNOME failure in LUKS run 31100096864.
        install = definition.get("install_packages")
        if install is not None:
            orphans = sorted(set(definition["required_packages"]) - set(install))
            if orphans:
                fail(f"{desktop}: required but never installed: {orphans}")
        for source in definition["sources"]:
            if len(source) != 1:
                fail(f"{desktop}: source has multiple kinds")
            if "local" in source and not (root / source["local"]).is_dir():
                fail(f"{desktop}: missing local source {source['local']}")
            if "fedora_distgit" in source and not source["fedora_distgit"].replace("-", "").isalnum():
                fail(f"{desktop}: invalid Fedora dist-git name")
            if "upstream_rpm" in source and not source["upstream_rpm"].startswith("https://"):
                fail(f"{desktop}: upstream source must use HTTPS")
    # Base-image component audit (#228): every component the Hummingbird base
    # image does not ship is attributed to the desktop edition that must
    # install it, and this is the enforcement.  A component missing from its
    # desktop's install_packages would never be installed, which is the exact
    # shape of the GNOME failure in LUKS run 31100096864.
    desktops = data.get("desktops", {})
    for desktop, components in data.get("components", {}).items():
        if desktop not in desktops:
            fail(f"components references unknown desktop {desktop}")
        install = set(desktops[desktop].get("install_packages") or [])
        missing = sorted(set(components) - install)
        if missing:
            fail(f"{desktop}: base-image audit components not in install_packages: {missing}")
    print("Hummingbird desktop catalog: valid")

if __name__ == "__main__":
    main()
