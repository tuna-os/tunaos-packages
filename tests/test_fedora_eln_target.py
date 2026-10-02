"""Tests for the Fedora ELN (EL11) build target.

Validates the target contract, mock configurations, dist tag derivation,
dependency catalog, and planner integration for Fedora ELN.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FACTORY_MANIFEST = ROOT / "manifests" / "package-factory.yaml"
MOCK_DIR = ROOT / "mock"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


catalog_builder = load_module("build_catalog", ROOT / "scripts" / "build-catalog.py")


def test_eln_target_contract_is_valid():
    data = yaml.safe_load(FACTORY_MANIFEST.read_text(encoding="utf-8"))
    targets = data.get("targets", {})
    assert "eln" in targets, "eln target must be declared in package-factory.yaml"

    eln = targets["eln"]
    assert eln.get("status") == "supported"
    assert eln.get("format") == "rpm"
    assert eln.get("buildroot") == "fedora-eln"
    assert set(eln.get("architectures", [])) == {"x86_64", "aarch64"}
    assert eln.get("r2_path") == "rpm/eln/{arch}"
    assert eln.get("probe_image") == "registry.fedoraproject.org/eln-bootc:latest"
    assert eln.get("build_repositories") == ["eln"]


def test_eln_mock_configs_exist_and_include_correct_base():
    x86_cfg = MOCK_DIR / "fedora-eln-ci.cfg"
    arm_cfg = MOCK_DIR / "fedora-eln-ci-aarch64.cfg"

    assert x86_cfg.is_file(), "mock/fedora-eln-ci.cfg must exist"
    assert arm_cfg.is_file(), "mock/fedora-eln-ci-aarch64.cfg must exist"

    x86_text = x86_cfg.read_text(encoding="utf-8")
    arm_text = arm_cfg.read_text(encoding="utf-8")

    assert "include('/etc/mock/fedora-eln-x86_64.cfg')" in x86_text
    assert "config_opts['root'] = 'fedora-eln-ci'" in x86_text
    assert "package_state_enable" in x86_text
    assert "local-build" in x86_text

    assert "include('/etc/mock/fedora-eln-aarch64.cfg')" in arm_text
    assert "config_opts['root'] = 'fedora-eln-ci-aarch64'" in arm_text
    assert "package_state_enable" in arm_text
    assert "local-build" in arm_text


def test_containerfile_copies_eln_mock_configs():
    containerfile = (MOCK_DIR / "Containerfile").read_text(encoding="utf-8")
    assert "COPY fedora-eln-ci.cfg /etc/mock/fedora-eln-ci.cfg" in containerfile
    assert "COPY fedora-eln-ci-aarch64.cfg /etc/mock/fedora-eln-ci-aarch64.cfg" in containerfile


def test_build_catalog_target_map_has_eln():
    assert catalog_builder.TARGET_MAP.get("fedora-eln-x86_64") == "eln"
    assert catalog_builder.TARGET_MAP.get("fedora-eln-aarch64") == "eln"


def test_build_chain_derives_eln_dist_tag(tmp_path):
    manifest = tmp_path / "test-manifest.yml"
    manifest.write_text("target: fedora-eln-x86_64\n")

    cmd = (
        'source <(sed -n "/^derive_dist()/,/^}/p" scripts/build-chain.sh); '
        f'MANIFEST="{manifest}" derive_dist'
    )
    result = subprocess.run(["bash", "-c", cmd], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    # Must match Fedora ELN's own %{dist} (rpm --eval %dist in an ELN image),
    # not the EL major: .eln11 sorted below ELN's .eln159 builds.
    assert result.stdout.strip() == ".eln159"


def test_dependency_catalog_covers_eln():
    data = yaml.safe_load(FACTORY_MANIFEST.read_text(encoding="utf-8"))
    catalog = data.get("dependency_catalog", {})
    assert catalog, "dependency_catalog must be present"
    for cap, mapping in catalog.items():
        assert "eln" in mapping, f"capability {cap} missing eln mapping"
        assert isinstance(mapping["eln"], list) and len(mapping["eln"]) > 0


def test_plan_package_factory_emits_eln_cells():
    result = subprocess.run(
        [sys.executable, "scripts/plan-package-factory.py", "--github-output", "/dev/null"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    matrices = [json.loads(m) for m in data.get("matrices", [])]
    cells = [cell for m in matrices for cell in m.get("include", [])]

    eln_cells = [c for c in cells if c.get("target") == "eln"]
    assert eln_cells, "planner must emit eln cells"
    eln_arches = {c.get("architecture") for c in eln_cells}
    assert eln_arches == {"x86_64", "aarch64"}


def test_plan_rpm_publish_eln():
    result = subprocess.run(
        [sys.executable, "scripts/plan-rpm-publish.py", "--target", "eln", "--packages", "uupd"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["target"] == "eln"
    assert plan["image"] == "registry.fedoraproject.org/eln-bootc:latest"
    assert sorted(plan["arches"]) == ["aarch64", "x86_64"]

    publish_rows = {row["arch"]: row for row in plan["publish"]["include"]}
    assert publish_rows["x86_64"]["src"] == "rpm/eln/x86_64"
    assert publish_rows["aarch64"]["src"] == "rpm/eln/aarch64"
    assert publish_rows["x86_64"]["served"] == "https://repo.tunaos.org/rpm/eln/x86_64/"
    assert publish_rows["aarch64"]["served"] == "https://repo.tunaos.org/rpm/eln/aarch64/"
