"""Tests for data-driven package-factory coverage."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_package_factory", ROOT / "scripts" / "validate-package-factory.py"
)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


DECLARED = {"el10", "ubuntu", "debian", "opensuse-tumbleweed", "arch"}


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_gate_targets_compatibility(tmp_path: Path) -> None:
    workflow = write(tmp_path, "gate.yml", "target: el10\ntarget: [ubuntu, debian]\n")
    assert validator.gate_targets([workflow]) == {"el10", "ubuntu", "debian"}


def test_matrix_targets_reads_recipes_and_native_registry(tmp_path: Path) -> None:
    write(tmp_path, "packages/demo/package.yaml", "targets: [el10, debian]\n")
    write(
        tmp_path,
        "manifests/package-builds.yaml",
        "native_builds:\n  - {id: desktop, target: fedora}\n",
    )
    assert validator.matrix_targets(tmp_path) == {"el10", "debian", "fedora"}


def test_check_coverage_rejects_uncovered_target() -> None:
    with pytest.raises(SystemExit):
        validator.check_coverage({"el10", "fedora"}, {"el10"})


def test_check_coverage_rejects_unknown_target() -> None:
    with pytest.raises(SystemExit):
        validator.check_coverage({"el10"}, {"el10", "mystery"})
