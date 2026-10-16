"""Fedora Rawhide is its own target, built in a Rawhide root (#831).

`fedora` builds in a fedora-44 root, so its packages cannot qualify
bonito-rawhide. These tests keep the two from collapsing back into one.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FACTORY = yaml.safe_load((ROOT / "manifests" / "package-factory.yaml").read_text(encoding="utf-8"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_rawhide_builds_in_a_rawhide_root_not_fedora_44():
    rawhide = FACTORY["targets"]["rawhide"]
    assert rawhide["format"] == "rpm"
    assert rawhide["buildroot"] == "fedora-rawhide"
    assert rawhide["probe_image"] == "registry.fedoraproject.org/fedora:rawhide"
    assert rawhide["r2_path"] == "rpm/rawhide/{arch}"
    assert FACTORY["targets"]["fedora"]["buildroot"] == "fedora-44"


def test_rawhide_publishes_into_its_own_prefix():
    planner = load_module("plan_rpm_publish", ROOT / "scripts" / "plan-rpm-publish.py")
    plan = planner.plan("rawhide", ["tuna-desktop"], None)
    rows = {row["arch"]: row for row in plan["publish"]["include"]}
    assert rows["x86_64"]["src"] == "rpm/rawhide/x86_64"
    assert rows["aarch64"]["served"] == "https://repo.tunaos.org/rpm/rawhide/aarch64/"
    assert plan["image"] == "registry.fedoraproject.org/fedora:rawhide"
