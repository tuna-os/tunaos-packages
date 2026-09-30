"""Compatibility-focused tests for official package metadata comparison."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("compare_official_package", ROOT / "scripts" / "compare-official-package.py")
assert SPEC and SPEC.loader
comparator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(comparator)


def recipe() -> dict:
    return {
        "name": "niri",
        "version": "26.04",
        "release": 1,
        "summary": "A compositor",
        "license": "GPL-3.0-or-later",
        "dependencies": {
            "runtime": {"targets": {"arch": ["glibc", "libinput>=1.0"]}},
            "build": {"common": ["rust"], "targets": {"arch": ["pkgconf"]}},
        },
    }


def official() -> dict:
    return {
        "pkgver": "26.04",
        "pkgrel": "1",
        "pkgdesc": "A different but equivalent description",
        "arch": "x86_64",
        "licenses": ["GPL-3.0-or-later"],
        "depends": ["glibc", "libinput>=1.0"],
        "makedepends": ["rust", "git"],
    }


def test_packager_choice_differences_are_advisory() -> None:
    report = comparator.compare_arch(recipe(), official(), "x86_64")
    assert report["build_dependencies"]["missing_from_recipe"] == ["git"]
    assert report["checks"]["description"]["matches"] is False
    assert comparator.has_divergence(report) is False


def test_missing_runtime_dependency_fails_compatibility_gate() -> None:
    current_recipe = recipe()
    current_recipe["dependencies"]["runtime"]["targets"]["arch"] = ["glibc"]
    report = comparator.compare_arch(current_recipe, official(), "x86_64")
    assert report["runtime_dependencies"]["missing_from_recipe"] == ["libinput"]
    assert comparator.has_divergence(report) is True
