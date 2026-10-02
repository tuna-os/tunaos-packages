"""Unit tests for scripts/check-upstream-shadowing.py.

Verifies target extraction, architecture derivation, and shadowing detection.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_upstream_shadowing",
    ROOT / "scripts" / "check-upstream-shadowing.py",
)
assert SPEC and SPEC.loader
shadowing_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shadowing_mod)


def mock_compare_evr(evr1: str, evr2: str) -> int:
    if evr1 == evr2:
        return 0
    # Simple version string comparison for test mock
    parts1 = [int(p) if p.isdigit() else p for p in evr1.replace("-", ".").split(".")]
    parts2 = [int(p) if p.isdigit() else p for p in evr2.replace("-", ".").split(".")]
    if parts1 < parts2:
        return -1
    elif parts1 > parts2:
        return 1
    return 0


def test_arch_of_derives_architecture() -> None:
    assert shadowing_mod.arch_of("hummingbird/snapshot-x86_64") == "x86_64"
    assert shadowing_mod.arch_of("gnome/20260901-aarch64/") == "aarch64"
    assert shadowing_mod.arch_of("custom/path", override="arm64") == "arm64"
    with pytest.raises(ValueError, match="cannot derive an architecture"):
        shadowing_mod.arch_of("invalid/path/format")


def test_targets_to_check_filters_valid_targets() -> None:
    factory = {
        "targets": {
            "target1": {
                "r2_path": "t1/snap-x86_64",
                "gap_measurement": {"target_index": "http://example.com/repo/$arch"},
            },
            "target2": {
                "r2_path": "t2/snap-aarch64",
            },
            "target3": {
                "gap_measurement": {"target_index": "http://example.com/repo/$arch"},
            },
        }
    }
    targets = shadowing_mod.targets_to_check(factory)
    assert len(targets) == 1
    assert targets[0][0] == "target1"

    filtered = shadowing_mod.targets_to_check(factory, only="target1")
    assert len(filtered) == 1

    empty = shadowing_mod.targets_to_check(factory, only="target2")
    assert len(empty) == 0


def test_shadowed_detects_older_served_packages() -> None:
    served = {
        "sudo": {"evr": "1.9.15-1.fc43", "arch": "x86_64"},
        "bash": {"evr": "5.2.21-1.fc43", "arch": "x86_64"},
        "libxkbcommon": {"evr": "1.6.0-1.fc43", "arch": "x86_64"},
        "src-pkg": {"evr": "1.0-1", "arch": "src"},
    }
    upstream = {
        "sudo": {"evr": "1.9.17-1.fc43", "arch": "x86_64"},
        "bash": {"evr": "5.2.21-1.fc43", "arch": "x86_64"},
        "libxkbcommon": {"evr": "1.5.0-1.fc43", "arch": "x86_64"},
        "src-pkg": {"evr": "2.0-1", "arch": "src"},
    }

    result = shadowing_mod.shadowed(served, upstream, mock_compare_evr)
    assert len(result) == 1
    assert result[0]["package"] == "sudo"
    assert result[0]["served_evr"] == "1.9.15-1.fc43"
    assert result[0]["upstream_evr"] == "1.9.17-1.fc43"
