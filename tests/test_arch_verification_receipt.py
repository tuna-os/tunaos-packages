"""Receipt contract tests; synthetic observations are not publication proof."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REVISION = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
SCRIPT = ROOT / "scripts/arch-verification-receipt.py"
SPEC = importlib.util.spec_from_file_location("receipt", SCRIPT)
receipt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipt)


@pytest.fixture
def evidence(tmp_path):
    provenance = tmp_path / "build"
    observations = tmp_path / "observed"
    provenance.mkdir()
    observations.mkdir()
    inputs = receipt.cache.action_inputs(
        argparse.Namespace(
            root=str(ROOT),
            recipe=str(ROOT / "packages/tuna-desktop/package.yaml"),
            factory=str(ROOT / "manifests/package-factory.yaml"),
            target="arch",
            arch="x86_64",
            image="registry.example/arch@sha256:" + "b" * 64,
            source_date_epoch=1700000000,
            dependency_key=[],
        )
    )
    key = receipt.cache.action_key(inputs)
    recipe = yaml.safe_load((ROOT / "packages/tuna-desktop/package.yaml").read_text())
    version = f"{recipe['version']}-{recipe['release']}"
    filename = f"tuna-desktop-{version}-x86_64.pkg.tar.zst"
    (provenance / "action-inputs.json").write_text(
        json.dumps({"action_key": key, "inputs": inputs})
    )
    result = {
        "schema": 1,
        "action_key": key,
        "artifacts": [{"name": filename, "size": 1234, "digest": "sha256:" + "a" * 64}],
    }
    (provenance / "action-result.json").write_text(json.dumps(result))
    (observations / "installed.tsv").write_text(
        f"tuna-desktop\t{version}\tx86_64\t{filename}\t{'a' * 64}\t1234\n"
    )
    (observations / "signature-status.txt").write_text(
        f"[GNUPG:] VALIDSIG {'C' * 40} 2026-10-10 1791640000 0 4 0 1 10 00 {receipt.SIGNER}\n"
    )
    (observations / "signature.sig").write_bytes(b"synthetic-unit-test-signature")
    return provenance, observations


def check(evidence, **overrides):
    arguments = dict(
        root=ROOT,
        provenance=evidence[0],
        observations=evidence[1],
        arch="x86_64",
        revision=REVISION,
        run_url="https://github.com/tuna-os/tunaos-packages/actions/runs/123",
        served_url="https://repo.tunaos.org/pacman/arch/x86_64",
    )
    arguments.update(overrides)
    return receipt.receipt(**arguments)


def test_exact_recipe_action_and_served_observations_form_a_receipt(evidence):
    value = check(evidence)
    assert value["status"] == "pass"
    assert value["package"]["sha256"] == "a" * 64
    assert value["signature"]["primary_fingerprint"] == receipt.SIGNER
    assert len(value["source_inputs"]) == 2
    assert "image boot" in value["note"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", "main"),
        ("run_url", "https://example.test/run/123"),
        ("served_url", "https://repo.tunaos.org/pacman/arch/aarch64"),
        ("arch", "aarch64"),
    ],
)
def test_wrong_revision_run_repository_or_architecture_cannot_pass(evidence, field, value):
    with pytest.raises(ValueError):
        check(evidence, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("action_key", "sha256:" + "e" * 64),
        ("schema", 99),
        ("artifacts", []),
        ("artifacts", [{"name": "../unsafe"}]),
    ],
)
def test_mismatched_or_missing_build_result_cannot_pass(evidence, field, value):
    path = evidence[0] / "action-result.json"
    result = json.loads(path.read_text())
    result[field] = value
    path.write_text(json.dumps(result))
    with pytest.raises((ValueError, KeyError, SystemExit)):
        check(evidence)


@pytest.mark.parametrize(
    "index,value",
    [
        (0, "roost"),
        (1, "0.1.0-999"),
        (2, "aarch64"),
        (3, "other.pkg.tar.zst"),
        (4, "f" * 64),
        (5, "999"),
    ],
)
def test_same_version_different_bytes_and_other_identity_mismatches_fail(evidence, index, value):
    path = evidence[1] / "installed.tsv"
    fields = path.read_text().strip().split("\t")
    fields[index] = value
    path.write_text("\t".join(fields) + "\n")
    with pytest.raises(ValueError):
        check(evidence)


def test_recipe_inputs_are_verified_not_merely_hashed(evidence):
    path = evidence[0] / "action-inputs.json"
    identity = json.loads(path.read_text())
    identity["inputs"]["recipe"]["tree"] = "sha256:" + "e" * 64
    identity["action_key"] = receipt.cache.action_key(identity["inputs"])
    path.write_text(json.dumps(identity))
    with pytest.raises(ValueError, match="current recipe"):
        check(evidence)


@pytest.mark.parametrize(
    "status", ["", "[GNUPG:] VALIDSIG " + "e" * 40, "[GNUPG:] EXPKEYSIG expired key\n"]
)
def test_unknown_expired_or_absent_signer_cannot_pass(evidence, status):
    (evidence[1] / "signature-status.txt").write_text(status)
    with pytest.raises(ValueError):
        check(evidence)


def test_validsig_does_not_override_expired_signature(evidence):
    path = evidence[1] / "signature-status.txt"
    path.write_text(path.read_text() + "[GNUPG:] EXPKEYSIG expired key\n")
    with pytest.raises(ValueError):
        check(evidence)


def test_missing_signature_file_cannot_pass(evidence):
    (evidence[1] / "signature.sig").unlink()
    with pytest.raises(ValueError):
        check(evidence)


def test_even_a_well_formed_revision_must_match_the_checkout(evidence):
    with pytest.raises(ValueError, match="verification checkout"):
        check(evidence, revision="f" * 40)


def test_truncated_validsig_with_expected_fingerprint_cannot_pass(evidence):
    (evidence[1] / "signature-status.txt").write_text(f"[GNUPG:] VALIDSIG {receipt.SIGNER}\n")
    with pytest.raises(ValueError):
        check(evidence)


def test_a_run_attempt_must_be_positive(evidence):
    with pytest.raises(ValueError):
        check(evidence, run_attempt=0)


def test_extra_debug_artifact_does_not_replace_the_installed_package(evidence):
    path = evidence[0] / "action-result.json"
    value = json.loads(path.read_text())
    value["artifacts"].append(
        {
            "name": "tuna-desktop-debug-0.1.0-1-x86_64.pkg.tar.zst",
            "size": 5678,
            "digest": "sha256:" + "e" * 64,
        }
    )
    path.write_text(json.dumps(value))
    assert check(evidence)["package"]["sha256"] == "a" * 64


def test_duplicate_main_package_artifacts_cannot_pass(evidence):
    path = evidence[0] / "action-result.json"
    value = json.loads(path.read_text())
    value["artifacts"].append(value["artifacts"][0])
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="duplicate"):
        check(evidence)


def test_failed_cli_recheck_removes_a_previous_passing_receipt(evidence, tmp_path):
    output = tmp_path / "verdict.json"
    output.write_text('{"status":"pass"}')
    (evidence[1] / "installed.tsv").unlink()
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(ROOT),
            "--provenance",
            str(evidence[0]),
            "--observations",
            str(evidence[1]),
            "--arch",
            "x86_64",
            "--revision",
            REVISION,
            "--run-url",
            "https://github.com/tuna-os/tunaos-packages/actions/runs/123",
            "--served-url",
            "https://repo.tunaos.org/pacman/arch/x86_64",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not output.exists()


def test_workflow_keeps_provenance_out_of_the_published_binary_wave():
    spec = yaml.safe_load((ROOT / ".github/workflows/publish-tideforge-arch.yml").read_text())
    publish = spec["jobs"]["publish"]["steps"]
    download = next(s for s in publish if s.get("uses", "").startswith("actions/download-artifact"))
    assert download["with"]["pattern"] == "publish-arch-${{ matrix.arch }}-*"
    assert not "publish-arch-provenance-".startswith("publish-arch-x86_64-")
    verify = spec["jobs"]["verify"]["steps"]
    assert any("arch-verification-receipt.py" in step.get("run", "") for step in verify)
    shell = (ROOT / "scripts/arch-verify-published.sh").read_text()
    assert shell.index(
        "pacman --config /tmp/tunaos-pacman.conf -S --noconfirm $names"
    ) < shell.index('--verify "$EVIDENCE_DIR/signature.sig"')


@pytest.mark.parametrize(
    "packages,selected",
    [
        ("tuna-desktop", True),
        (" \ttuna-desktop\n", True),
        ("other,\ntuna-desktop\t,", True),
        ("other, tuna-desktop-debug", False),
        ("\tother\n", False),
    ],
)
def test_workflow_receipt_guard_uses_the_planner_package_normalization(packages, selected):
    spec = yaml.safe_load((ROOT / ".github/workflows/publish-tideforge-arch.yml").read_text())
    step = next(
        step
        for step in spec["jobs"]["verify"]["steps"]
        if step.get("name") == "Bind Tuna Desktop served bytes to the verified build"
    )
    setup = step["run"].split('provenance="build-provenance/', 1)[0]
    result = subprocess.run(
        ["bash", "-c", setup + "\n  printf selected\nfi\n"],
        cwd=ROOT,
        env={
            **os.environ,
            "PACKAGES": packages,
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert not result.stderr
    assert (result.stdout == "selected") is selected
