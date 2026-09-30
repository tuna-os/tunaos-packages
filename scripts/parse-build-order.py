#!/usr/bin/env python3
"""
Parse build-order.yml and output tier/package info for shell consumption.

Usage:
    parse-build-order.py build-order.yml                  # list all tiers
    parse-build-order.py build-order.yml --tier <name>    # list packages in tier
    parse-build-order.py build-order.yml --tiers          # list tier names only
"""

import argparse
import sys

import yaml


def validate_manifest(manifest_path):
    import json
    import os

    import jsonschema

    schema_path = os.path.join(os.path.dirname(manifest_path), "build-order-schema.json")
    if not os.path.exists(schema_path):
        schema_path = "build-order-schema.json"
    if not os.path.exists(schema_path):
        print("    Schema file not found. Skipping validation.")
        return

    with open(manifest_path) as fh:
        data = yaml.safe_load(fh)
    with open(schema_path) as fh:
        schema = json.load(fh)
    jsonschema.validate(data, schema)
    print("    Schema valid")


def main():
    parser = argparse.ArgumentParser(description="Parse build-order.yml")
    parser.add_argument("manifest", help="Path to build-order.yml")
    parser.add_argument("--tier", help="Print packages for a specific tier")
    parser.add_argument(
        "--tiers", action="store_true", help="Print tier names only"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Print all packages from all tiers in order (tab-separated path\\tspec)"
    )
    parser.add_argument(
        "--tiers-filter", default="",
        help="Comma-separated tier names; when set, --all only outputs these tiers"
    )
    parser.add_argument(
        "--validate", action="store_true", help="Validate manifest against schema"
    )
    args = parser.parse_args()

    if args.validate:
        validate_manifest(args.manifest)
        return

    with open(args.manifest) as f:
        data = yaml.safe_load(f)

    if args.tiers:
        for tier in data["tiers"]:
            print(tier["name"])
        return

    if args.all:
        allowed = set(t.strip() for t in args.tiers_filter.split(",") if t.strip()) if args.tiers_filter else None
        for tier in data["tiers"]:
            if allowed and tier["name"] not in allowed:
                continue
            for pkg in tier.get("packages", []):
                if "path" not in pkg:
                    continue
                spec = pkg.get("spec_override", "")
                print(f"{pkg['path']}\t{spec}")
        return

    if args.tier:
        for tier in data["tiers"]:
            if tier["name"] == args.tier:
                for pkg in tier.get("packages", []):
                    if "path" not in pkg:
                        continue  # skip copr_name-only entries
                    spec = pkg.get("spec_override", "")
                    print(f"{pkg['path']}\t{spec}")
                return
        print(f"Error: tier '{args.tier}' not found", file=sys.stderr)
        sys.exit(1)

    # Default: print all tiers and their packages
    for tier in data["tiers"]:
        print(f"=== {tier['name']} ===")
        for pkg in tier.get("packages", []):
            if "path" not in pkg:
                continue  # skip copr_name-only entries
            spec = pkg.get("spec_override", "")
            line = pkg["path"]
            if spec:
                line += f"\t({spec})"
            print(f"  {line}")


if __name__ == "__main__":
    main()
