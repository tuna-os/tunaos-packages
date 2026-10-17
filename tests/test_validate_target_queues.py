"""Tests for target queue contract validation."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("validate_target_queues", ROOT / "scripts" / "validate-target-queues.py")
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def write_yaml(path: Path, contents: str) -> None:
    path.write_text(contents)


def test_repository_target_queues_are_valid() -> None:
    for queue_path in sorted((ROOT / "manifests/target-queues").glob("*.yaml")):
        validator.validate_queue(queue_path, ROOT / "manifests/dependency-trees" / queue_path.name, ROOT)


def test_rejects_missing_queue_for_tree_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    queue = tmp_path / "niri.yaml"
    tree = tmp_path / "tree.yaml"
    write_yaml(queue, "schema: 1\nqueues:\n  el10:\n    format: rpm\n    implementation: native-spec\n    roots: [niri]\n    gates: [mock-build]\n")
    write_yaml(tree, "schema: 1\ntargets: [el10, debian]\nnodes:\n  niri: {}\n")

    with pytest.raises(SystemExit):
        validator.validate_queue(queue, tree, tmp_path)
    assert "missing queues" in capsys.readouterr().err


def test_rejects_incompatible_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    queue = tmp_path / "niri.yaml"
    tree = tmp_path / "tree.yaml"
    write_yaml(queue, "schema: 1\nqueues:\n  el10:\n    format: deb\n    implementation: native-spec\n    suite: trixie\n    roots: [niri]\n    gates: [mock-build]\n")
    write_yaml(tree, "schema: 1\ntargets: [el10]\nnodes:\n  niri: {}\n")

    with pytest.raises(SystemExit):
        validator.validate_queue(queue, tree, tmp_path)
    assert "incompatible" in capsys.readouterr().err
