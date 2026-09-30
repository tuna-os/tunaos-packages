#!/usr/bin/env python3
"""Validate the package-factory contract and its data-driven matrix coverage."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_mapping(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        fail(f"{path}: expected a YAML mapping")
    return data


def gate_targets(workflows: list[Path]) -> set[str]:
    """Compatibility helper for tests and downstream callers of the old API."""
    exercised: set[str] = set()
    for workflow in workflows:
        if not workflow.exists():
            continue
        text = workflow.read_text(encoding="utf-8")
        for match in re.finditer(r"--target\s+\"?([a-z0-9-]+)\b", text):
            exercised.add(match.group(1))
        for match in re.finditer(r"^\s*target:\s*([a-z0-9-]+)\s*$", text, re.MULTILINE):
            exercised.add(match.group(1))
        for match in re.finditer(r"^\s*target:\s*\[([^\]]+)\]\s*$", text, re.MULTILINE):
            for name in match.group(1).split(","):
                name = name.strip().strip("\"'")
                if re.fullmatch(r"[a-z0-9-]+", name):
                    exercised.add(name)
    return exercised


def check_gate_coverage(targets: set[str], workflows: list[Path]) -> None:
    check_coverage(targets, gate_targets(workflows))


def matrix_targets(root: Path) -> set[str]:
    """Discover target coverage from recipe and native-build data, not workflow YAML."""
    exercised: set[str] = set()
    for recipe_path in sorted((root / "packages").glob("*/package.yaml")):
        recipe = load_mapping(recipe_path)
        declared = recipe.get("targets") or []
        if not isinstance(declared, list):
            fail(f"{recipe_path}: targets must be a list")
        exercised.update(str(target) for target in declared)
    registry_path = root / "manifests" / "package-builds.yaml"
    if registry_path.exists():
        registry = load_mapping(registry_path)
        for build in registry.get("native_builds") or []:
            if not isinstance(build, dict) or not build.get("target"):
                fail(f"{registry_path}: every native build requires a target")
            exercised.add(str(build["target"]))
    return exercised


def check_coverage(targets: set[str], exercised: set[str]) -> None:
    unknown = sorted(exercised - targets)
    if unknown:
        fail(f"matrix data references undeclared target(s): {unknown}")
    uncovered = sorted(targets - exercised)
    if uncovered:
        fail(f"declared target(s) with zero matrix cells: {uncovered}")
    print(f"Matrix coverage: all {len(targets)} declared targets are exercised")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--gate-workflow", type=Path, action="append", default=None)
    args = parser.parse_args()
    data = load_mapping(args.manifest)
    if data.get("schema") != 1:
        fail("schema must be 1")

    seen_upstreams: set[str] = set()
    for upstream in data.get("upstreams", []):
        upstream_id = upstream.get("id", "")
        if not upstream_id or upstream_id in seen_upstreams:
            fail(f"invalid or duplicate upstream id: {upstream_id!r}")
        if not upstream.get("url", "").startswith("https://"):
            fail(f"{upstream_id}: URL must use HTTPS")
        if upstream.get("license_review") != "required":
            fail(f"{upstream_id}: license_review must be required")
        seen_upstreams.add(upstream_id)

    targets = data.get("targets", {})
    if not isinstance(targets, dict) or not targets:
        fail("targets must be a non-empty mapping")
    for target_id, target in targets.items():
        if target.get("status") not in {"supported", "scaffold"}:
            fail(f"{target_id}: status must be supported or scaffold")
        for field in ("format", "architectures", "r2_path", "repository", "probe_image"):
            if not target.get(field):
                fail(f"{target_id}: {field} is required")
        if "/" not in target["probe_image"]:
            fail(f"{target_id}: probe_image must be a fully-qualified container image")
        if not target["r2_path"].startswith(("rpm/", "apt/", "pacman/", "hummingbird/", "xfce/")):
            fail(f"{target_id}: r2_path has an unsupported namespace")
        if not all(arch in {"x86_64", "aarch64", "amd64", "arm64"} for arch in target["architectures"]):
            fail(f"{target_id}: unsupported architecture")
        repositories = target.get("build_repositories", [])
        if not isinstance(repositories, list) or not all(isinstance(item, str) and item for item in repositories):
            fail(f"{target_id}: build_repositories must be a list of non-empty names")

    print("Package factory manifest: valid")
    root = args.manifest.parent.parent
    if args.gate_workflow:
        check_gate_coverage(set(targets), args.gate_workflow)
    else:
        check_coverage(set(targets), matrix_targets(root))


if __name__ == "__main__":
    main()
