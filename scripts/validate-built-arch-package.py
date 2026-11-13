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


def validate_native(recipe: dict, info: str, native: dict) -> None:
    """Every native split output must exist, agree in version and obey admission."""
    records = [record for record in re.split(r"\n[ \t]*\n", info.strip()) if record.strip()]
    packages = {}
    for record in records:
        name = field(record, "Name")
        if name in packages:
            fail("built native Arch metadata repeats a package")
        packages[name] = record
    required = set(native["packages"])
    permitted = required | set(native.get("optional_packages", []))
    if not required <= set(packages) or not set(packages) <= permitted:
        fail("built native Arch outputs do not match the complete split package set")
    expected_version = f"{tideforge.pacman_version(recipe['version'])}-{recipe.get('release', 1)}"
    allowed = tideforge.factory_contract.recipe_architectures(
        recipe, "arch", tideforge.load_targets()["arch"]
    )
    observed_architectures = set()
    for record in packages.values():
        if field(record, "Version") != expected_version:
            fail("built native Arch split package version does not match the recipe")
        architecture = field(record, "Architecture")
        if architecture not in allowed:
            fail("built native Arch split package has an unadmitted architecture")
        observed_architectures.add(architecture)
    if len(observed_architectures) != 1:
        fail("built native Arch split packages disagree on architecture")
    main = packages[recipe["name"]]
    dependencies = re.search(r"^Depends On[ \t]*:[ \t]*([^\n]*(?:\n[ \t]+[^\n]*)*)", main, re.MULTILINE)
    if dependencies is None:
        fail("built native Arch metadata is missing Depends On")
    built = set(dependencies.group(1).split())
    built_names = dependency_names(list(built))
    for dependency in tideforge.target_runtime_dependencies(recipe, "arch"):
        if ((re.search(r"[<>=]", dependency) and dependency not in built)
                or re.split(r"[<>=]", dependency, maxsplit=1)[0] not in built_names):
            fail(f"built native Arch package is missing exact runtime requirement: {dependency}")


def validate(recipe: dict, info: str) -> None:
    native = tideforge.native_arch_pkgbuild(recipe)
    if native is not None:
        validate_native(recipe, info, native)
        print("Built native Arch split packages: valid")
        return
    if field(info, "Name") != recipe["name"]:
        fail("built package name does not match recipe")
    if not field(info, "Version").startswith(f"{tideforge.pacman_version(recipe['version'])}-"):
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
