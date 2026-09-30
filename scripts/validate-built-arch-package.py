#!/usr/bin/env python3
"""Validate an Arch package artifact against the runtime contract in a recipe."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "manifests" / "package-factory.yaml"
sys.path.insert(0, str(ROOT / "scripts"))
import tideforge  # noqa: E402


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def field(info: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\s*:\s*(.+)$", info, re.MULTILINE)
    if not match:
        fail(f"built package metadata is missing {name}")
    return match.group(1).strip()


def dependency_names(values: list[str]) -> set[str]:
    return {re.split(r"[<>=: ]", value, maxsplit=1)[0] for value in values}


def runtime_dependencies(recipe: dict) -> set[str]:
    """Resolve both native and capability runtime requirements for Arch."""
    runtime = recipe.get("dependencies", {}).get("runtime", {})
    dependencies = list(runtime.get("common", [])) + list(runtime.get("targets", {}).get("arch", []))
    catalog_data = yaml.safe_load(CATALOG.read_text())
    catalog = catalog_data.get("dependency_catalog", {}) if isinstance(catalog_data, dict) else {}
    for capability in runtime.get("capabilities", []):
        mapping = catalog.get(capability, {})
        packages = mapping.get("arch") if isinstance(mapping, dict) else None
        if not isinstance(packages, list) or not all(isinstance(package, str) for package in packages):
            fail(f"runtime capability has no Arch mapping: {capability}")
        dependencies.extend(packages)
    return dependency_names(dependencies)


def validate(recipe: dict, info: str) -> None:
    if field(info, "Name") != recipe["name"]:
        fail("built package name does not match recipe")
    if not field(info, "Version").startswith(f"{recipe['version']}-"):
        fail("built package version does not match recipe")
    allowed_architectures = {"x86_64", "aarch64"}
    contract = tideforge.portable_payload_contract(recipe)
    if contract and contract.get("architecture") == "noarch":
        allowed_architectures.add("any")
    if field(info, "Architecture") not in allowed_architectures:
        fail("built package has an unsupported architecture")
    declared = runtime_dependencies(recipe)
    built = dependency_names(field(info, "Depends On").split())
    missing = declared - built
    if missing:
        fail(f"built package is missing declared runtime dependencies: {sorted(missing)}")
    print("Built Arch package: valid")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe", type=Path)
    parser.add_argument("package_info", type=Path)
    args = parser.parse_args()
    recipe = yaml.safe_load(args.recipe.read_text())
    if not isinstance(recipe, dict):
        fail("recipe must be a mapping")
    validate(recipe, args.package_info.read_text())


if __name__ == "__main__":
    main()
