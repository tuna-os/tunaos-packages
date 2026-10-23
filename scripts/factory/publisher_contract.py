"""Shared contract validation for format-specific publisher planners."""
from __future__ import annotations

import pathlib
from typing import Any

from . import factory_contract
import yaml
from .target_platform import build_context  # noqa: F401  (re-exported for plan-rpm-publish.py)

ROOT = pathlib.Path(__file__).resolve().parents[2]
FACTORY = factory_contract.FACTORY


# These targets have native candidate inputs, but no production CPU/closure
# qualification yet. This deliberate denylist is independent of status/r2_path:
# supported build targets and a declared destination do not authorize promotion.
# Remove a target only in a reviewed production-admission change.
CANDIDATE_ONLY_RPM_TARGETS = frozenset({"alma10", "alma10-kitten"})


def production_rejection(name: str, destination: str = "") -> str | None:
    candidate = name if name in CANDIDATE_ONLY_RPM_TARGETS else None
    # Also protect the existing Alma repository namespaces if a catalog cell
    # accidentally drops/aliases its target. A destination never grants release
    # authority, even when the target field incorrectly names a qualified lane.
    for segment in destination.strip("/").split("/"):
        if segment == "alma10" or segment.startswith("alma10-"):
            candidate = candidate or segment
    if candidate:
        return (f"target/destination {candidate} is candidate-only; "
                "production publication is forbidden "
                "pending source-bound CPU and dependency-closure qualification")
    return None


def split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def target(name: str, expected_format: str, fail: Any,
           factory: pathlib.Path = FACTORY) -> dict[str, Any]:
    try:
        targets = factory_contract.load_targets(factory)
    except factory_contract.ContractError as error:
        fail(str(error))
        raise AssertionError("fail callback returned")
    spec = targets.get(name)
    if not spec:
        fail(f"unknown target {name!r}; the contract declares {sorted(targets)}")
    if spec.get("format") != expected_format:
        fail(f"target {name} is format {spec.get('format')!r}, not {expected_format}")
    if not spec.get("probe_image") and not spec.get("probe_images"):
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
