"""Shared contract validation for format-specific publisher planners."""
from __future__ import annotations

import pathlib
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
FACTORY = ROOT / "manifests" / "package-factory.yaml"


def split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def target(name: str, expected_format: str, fail: Any) -> dict[str, Any]:
    targets = (yaml.safe_load(FACTORY.read_text(encoding="utf-8")) or {}).get("targets") or {}
    spec = targets.get(name)
    if not spec:
        fail(f"unknown target {name!r}; the contract declares {sorted(targets)}")
    if spec.get("format") != expected_format:
        fail(f"target {name} is format {spec.get('format')!r}, not {expected_format}")
    if not spec.get("probe_image"):
        fail(f"target {name} declares no probe_image to build in")
    return spec


def architectures(name: str, spec: dict[str, Any], selected: list[str] | None,
                  runners: dict[str, str], fail: Any) -> list[str]:
    declared = list(spec.get("architectures") or [])
    if not declared:
        fail(f"target {name} declares no architectures")
    result = selected or declared
    missing = sorted(set(result) - set(declared))
    if missing:
        fail(f"{name} does not declare arch(es) {missing}; it declares {declared}")
    unrunnable = sorted(set(result) - set(runners))
    if unrunnable:
        fail(f"no runner for arch(es) {unrunnable}")
    return result


def recipe(package: str, target_name: str, fail: Any) -> dict[str, Any]:
    path = ROOT / "packages" / package / "package.yaml"
    if not path.is_file():
        fail(f"no recipe at packages/{package}/package.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if target_name not in (data.get("targets") or []):
        fail(f"{package} does not target {target_name}; refusing to publish it there")
    return data
