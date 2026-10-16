"""Publication preparation preserves authenticated inputs and inactive scope."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "packaging/tiny-dfr/input-contract.json"
SCRIPT = ROOT / "scripts/prepare-tiny-dfr-publication-ci.sh"


def test_pending_recipe_binds_actual_ci_capture_and_offline_native_build():
    receipt = json.loads(CONTRACT.read_text())
    recipe = yaml.safe_load((ROOT / "packaging/tiny-dfr/package.yaml").read_text())
    assert receipt["producer"]["runId"] == 38085707264
    assert receipt["producer"]["sourceRevision"] == "dd0c94685ca05e6232e8e85bc210713c66768155"
    assert recipe["source"]["sha256"] == receipt["artifacts"]["tiny-dfr-source.tar.gz"]["sha256"]
    assert recipe["source"]["url"] == receipt["upstream"]["url"]
    assert recipe["sources"][0]["sha256"] == receipt["artifacts"]["tiny-dfr-vendor.tar.gz"]["sha256"]
    assert recipe["license"] == "MIT AND Apache-2.0"
    assert "cargo build --release --locked --offline" in recipe["build"]["commands"]
    assert recipe["targets"] == ["opensuse-tumbleweed"]
    assert not (ROOT / "packages/tiny-dfr/package.yaml").exists()
    assert receipt["readiness"] is False
    assert receipt["target"]["cpuCompatibilityVerified"] is False


@pytest.mark.parametrize("problem", [None, "digest", "size", "symlink", "missing", "path", "scope", "receipt"])
def test_exact_receipt_artifact_gate_real_filesystem(tmp_path, problem):
    source = SCRIPT.read_text()
    body = re.search(r"<<'PY'\n(.*?)\nPY", source, re.S)[1]
    assets = tmp_path / "assets"
    assets.mkdir()
    expected_dir = tmp_path / "packaging/tiny-dfr"
    expected_dir.mkdir(parents=True)
    data = b"fixture source input"
    name = "source.tar.gz"
    receipt = {"artifacts": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}},
               "readiness": False, "target": {"cpuCompatibilityVerified": False}}
    (assets / name).write_bytes(data)
    if problem == "digest":
        (assets / name).write_bytes(b"tampered")
    elif problem == "size":
        receipt["artifacts"][name]["size"] += 1
    elif problem == "symlink":
        other = tmp_path / "outside"
        other.write_bytes(data)
        (assets / name).unlink()
        (assets / name).symlink_to(other)
    elif problem == "missing":
        (assets / name).unlink()
    elif problem == "path":
        receipt["artifacts"]["../outside"] = receipt["artifacts"].pop(name)
    elif problem == "scope":
        receipt["readiness"] = True
    (expected_dir / "input-contract.json").write_text(json.dumps(receipt))
    if problem == "receipt":
        receipt["extra"] = "different unreviewed producer"
    (assets / "input-contract.json").write_text(json.dumps(receipt))
    result = subprocess.run(["python3", "-c", body, str(assets)], cwd=tmp_path,
                            text=True, capture_output=True)
    assert (result.returncode == 0) is (problem is None)


def test_review_workflow_cannot_publish_or_enable_package():
    workflow = yaml.safe_load((ROOT / ".github/workflows/tiny-dfr-source-publication.yml").read_text())
    assert set(workflow["permissions"].values()) == {"read"}
    assert all("permissions" not in job for job in workflow["jobs"].values())
    source = SCRIPT.read_text()
    assert "gh release" not in source
    assert "--source-digest" in source
    assert "--signer-workflow" in source
    assert ".conclusion == \"success\"" in source
    assert ".head_repository.full_name == $repo" in source
    assert "packages/tiny-dfr" not in source
