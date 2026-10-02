"""Tests for Arch artifact runtime-dependency validation."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("validate_built_arch_package", ROOT / "scripts" / "validate-built-arch-package.py")
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


RECIPE = {"name": "niri", "version": "26.04", "dependencies": {"runtime": {"targets": {"arch": ["glibc", "libinput>=1.0"]}}}}
INFO = """Name            : niri
Version         : 26.04-1
Architecture    : x86_64
Depends On      : glibc  libinput>=1.0
"""


def test_accepts_built_package_with_declared_runtime_dependencies() -> None:
    validator.validate(RECIPE, INFO)


def test_rejects_missing_runtime_dependency() -> None:
    with pytest.raises(SystemExit):
        validator.validate(RECIPE, INFO.replace("  libinput>=1.0", ""))


def test_resolves_runtime_capabilities_from_the_package_factory_catalog() -> None:
    recipe = {
        "name": "niri",
        "version": "26.04",
        "dependencies": {"runtime": {"capabilities": ["dbus"]}},
    }
    validator.validate(recipe, INFO.replace("glibc  libinput>=1.0", "dbus"))


def test_rejects_runtime_capability_missing_from_arch_metadata() -> None:
    recipe = {
        "name": "niri",
        "version": "26.04",
        "dependencies": {"runtime": {"capabilities": ["dbus"]}},
    }
    with pytest.raises(SystemExit):
        validator.validate(recipe, INFO.replace("glibc  libinput>=1.0", ""))
