#!/usr/bin/env python3
"""Compare a Tideforge recipe with an official distribution package.

The first adapter targets Arch's public package API.  It compares the package
metadata that can break a user installation (version, architecture, and runtime
dependencies) without treating a distribution maintainer's patches, build
dependencies, release number, or packaging preferences as parity requirements.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.request import urlopen

import yaml

ARCH_API = "https://archlinux.org/packages/extra/{architecture}/{package}/json/"


def package_name(dependency: str) -> str:
    return re.split(r"[<>=: ]", dependency, maxsplit=1)[0]


def recipe_dependencies(recipe: dict, section: str, target: str) -> set[str]:
    dependencies = recipe.get("dependencies", {}).get(section, {})
    values = list(dependencies.get("common", [])) + list(dependencies.get("targets", {}).get(target, []))
    return {package_name(value) for value in values}


def fetch_arch_package(package: str, architecture: str) -> dict:
    with urlopen(ARCH_API.format(package=package, architecture=architecture), timeout=30) as response:
        return json.load(response)


def compare_arch(recipe: dict, official: dict, architecture: str) -> dict:
    expected_runtime = recipe_dependencies(recipe, "runtime", "arch")
    expected_build = recipe_dependencies(recipe, "build", "arch")
    official_runtime = {package_name(value) for value in official.get("depends", [])}
    official_build = {package_name(value) for value in official.get("makedepends", [])}
    checks = {
        "version": {"expected": recipe["version"], "official": official["pkgver"], "matches": recipe["version"] == official["pkgver"]},
        "release": {"expected": str(recipe.get("release", 1)), "official": official["pkgrel"], "matches": str(recipe.get("release", 1)) == official["pkgrel"]},
        "description": {"expected": recipe["summary"], "official": official["pkgdesc"], "matches": recipe["summary"] == official["pkgdesc"]},
        "architecture": {"expected": architecture, "official": official["arch"], "matches": architecture == official["arch"]},
        "license": {"expected": recipe["license"], "official": official.get("licenses", []), "matches": recipe["license"] in official.get("licenses", [])},
    }
    return {
        "target": "arch",
        "package": recipe["name"],
        "official_url": ARCH_API.format(package=recipe["name"], architecture=architecture),
        "checks": checks,
        "runtime_dependencies": {
            "declared": sorted(expected_runtime),
            "official": sorted(official_runtime),
            "missing_from_recipe": sorted(official_runtime - expected_runtime),
            "extra_in_recipe": sorted(expected_runtime - official_runtime),
        },
        "build_dependencies": {
            "declared": sorted(expected_build),
            "official": sorted(official_build),
            "missing_from_recipe": sorted(official_build - expected_build),
            "extra_in_recipe": sorted(expected_build - official_build),
        },
    }


def has_divergence(report: dict) -> bool:
    required_checks = ("version", "architecture")
    return any(not report["checks"][check]["matches"] for check in required_checks) or bool(
        report["runtime_dependencies"]["missing_from_recipe"]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe", type=Path)
    parser.add_argument("--target", choices=["arch"], default="arch")
    parser.add_argument("--architecture", default="x86_64")
    parser.add_argument("--strict", action="store_true", help="fail only on compatibility-relevant divergence")
    args = parser.parse_args()
    recipe = yaml.safe_load(args.recipe.read_text())
    if not isinstance(recipe, dict) or args.target not in recipe.get("targets", []):
        raise SystemExit(f"ERROR: {args.recipe} does not enable {args.target}")
    report = compare_arch(recipe, fetch_arch_package(recipe["name"], args.architecture), args.architecture)
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.strict and has_divergence(report):
        raise SystemExit("ERROR: rendered package metadata diverges from the official package")


if __name__ == "__main__":
    main()
