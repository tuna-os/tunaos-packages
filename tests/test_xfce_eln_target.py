"""Tests for XFCE on Fedora ELN (EL11).

Issue tuna-os/tunaos-packages#561:
XFCE for Fedora ELN — a new target for the xfce family we already build, not a new family.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILDS = ROOT / "manifests" / "package-builds.yaml"
FACTORY = ROOT / "manifests" / "package-factory.yaml"
TARGET_QUEUES = ROOT / "manifests" / "target-queues" / "xfce.yaml"
DEP_TREES = ROOT / "manifests" / "dependency-trees" / "xfce.yaml"


def native_cells() -> dict:
    spec = yaml.safe_load(BUILDS.read_text(encoding="utf-8"))
    entries = spec["native_builds"] if "native_builds" in spec else next(v for v in spec.values() if isinstance(v, list))
    return {c["id"]: c for c in entries}


def test_xfce_eln_cells_exist():
    cells = native_cells()
    assert "xfce-eln-x86_64" in cells
    assert "xfce-eln-aarch64" in cells


def test_xfce_eln_cells_share_existing_family_and_manifest():
    cells = native_cells()
    x86 = cells["xfce-eln-x86_64"]
    arm = cells["xfce-eln-aarch64"]

    assert x86["family"] == "xfce"
    assert arm["family"] == "xfce"
    assert x86["target"] == "eln"
    assert arm["target"] == "eln"
    assert x86["manifest"] == "build-order-xfce.yml"
    assert arm["manifest"] == "build-order-xfce.yml"


def test_xfce_eln_cells_use_correct_mock_configs():
    cells = native_cells()
    assert cells["xfce-eln-x86_64"]["mock_config"] == "fedora-eln-ci"
    assert cells["xfce-eln-aarch64"]["mock_config"] == "fedora-eln-ci-aarch64"
    assert (ROOT / "mock" / "fedora-eln-ci.cfg").is_file()
    assert (ROOT / "mock" / "fedora-eln-ci-aarch64.cfg").is_file()


def test_xfce_eln_cells_use_eln_bootc_verify_image():
    cells = native_cells()
    image = "registry.fedoraproject.org/eln-bootc:latest"
    assert cells["xfce-eln-x86_64"]["verify_image"] == image
    assert cells["xfce-eln-aarch64"]["verify_image"] == image


def test_xfce_eln_cells_publish_to_distinct_r2_prefixes():
    cells = native_cells()
    x86_r2 = cells["xfce-eln-x86_64"]["r2_path"]
    arm_r2 = cells["xfce-eln-aarch64"]["r2_path"]
    assert x86_r2 == "xfce/eln-x86_64"
    assert arm_r2 == "xfce/eln-aarch64"
    assert x86_r2 != arm_r2


def test_target_queue_and_dependency_tree_declare_eln():
    queue_data = yaml.safe_load(TARGET_QUEUES.read_text(encoding="utf-8"))
    tree_data = yaml.safe_load(DEP_TREES.read_text(encoding="utf-8"))

    assert "eln" in queue_data["queues"]
    assert queue_data["queues"]["eln"]["build_order"] == "build-order-xfce.yml"
    assert queue_data["queues"]["eln"]["format"] == "rpm"
    assert queue_data["queues"]["eln"]["implementation"] == "native-spec"
    assert "eln" in tree_data["targets"]


def test_build_chain_derives_eln_dist_with_mock_config():
    result = subprocess.run(
        [
            ROOT / "scripts" / "build-chain.sh",
            "--manifest",
            "build-order-xfce.yml",
            "--mock-config",
            "fedora-eln-ci",
            "--print-dist",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ".eln159"


def test_plan_build_chain_publish_resolves_xfce_eln_cells():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "plan-build-chain-publish.py"),
            "--cells",
            "xfce-eln-x86_64,xfce-eln-aarch64",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout.strip().splitlines()[-1])
    ids = {c["id"] for c in data["include"]}
    assert ids == {"xfce-eln-x86_64", "xfce-eln-aarch64"}
