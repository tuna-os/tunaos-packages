"""Receipt tests bind Debian bytes, signatures and installed closure."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deb-verification-receipt.py"
SPEC = importlib.util.spec_from_file_location("deb_receipt", SCRIPT)
assert SPEC.loader is not None
receipt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipt)
REVISION = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def action_identity() -> tuple[dict, str, dict]:
    inputs = receipt.cache.action_inputs(
        argparse.Namespace(
            root=str(ROOT),
            recipe=str(ROOT / "packages/tuna-desktop/package.yaml"),
            factory=str(ROOT / "manifests/package-factory.yaml"),
            target="ubuntu",
            arch="amd64",
            image="registry.example/ubuntu@sha256:" + "b" * 64,
            source_date_epoch=1700000000,
            dependency_key=[],
            consumer_bindings=None,
        )
    )
    key = receipt.cache.action_key(inputs)
    recipe = yaml.safe_load((ROOT / "packages/tuna-desktop/package.yaml").read_text(encoding="utf-8"))
    return {"action_key": key, "inputs": inputs}, key, recipe


@pytest.fixture
def evidence(tmp_path: Path) -> tuple[Path, Path]:
    provenance = tmp_path / "provenance"
    observations = tmp_path / "observations"
    observed = observations / "tuna-desktop"
    provenance.mkdir()
    observed.mkdir(parents=True)
    identity, key, recipe = action_identity()
    (provenance / "action-inputs.json").write_text(json.dumps(identity), encoding="utf-8")
    version = f"{recipe['version']}-{recipe['release']}"
    filename = f"tuna-desktop_{version}_amd64.deb"
    digest = "a" * 64
    size = "1234"
    (provenance / "action-result.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "action_key": key,
                "artifacts": [{"name": filename, "size": int(size), "digest": "sha256:" + digest}],
            }
        ),
        encoding="utf-8",
    )
    (observations / "InRelease").write_bytes(b"synthetic signed InRelease")
    (observations / "signature-status.txt").write_text(
        f"[GNUPG:] VALIDSIG {receipt.SIGNER} 2026-10-10 1791640000 0 4 0 1 10 00 {receipt.SIGNER}\n",
        encoding="utf-8",
    )
    (observed / "installed.tsv").write_text(
        f"tuna-desktop\t{version}\tamd64\t{filename}\t{digest}\t{size}\n",
        encoding="utf-8",
    )
    (observed / "index-entry.txt").write_text(
        "\n".join(
            [
                "Package: tuna-desktop",
                f"Version: {version}",
                "Architecture: amd64",
                f"Filename: pool/t/tuna-desktop/{filename}",
                f"SHA256: {digest}",
                f"Size: {size}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    common = set(recipe["files"]["common"])
    payload = ["archive\t" + digest]
    for path in sorted(common):
        if path.startswith("usr/bin/roost-"):
            payload.append(
                "symlink\t"
                + path
                + "\t"
                + path.rsplit("/", 1)[1].replace("roost-", "tuna-")
            )
        else:
            mode = "755" if path.startswith("usr/bin/tuna-") else "644"
            payload.append(f"file\t{path}\t{mode}\t{'c' * 64}")
    (observed / "installed-payload.tsv").write_text("\n".join(payload) + "\n", encoding="utf-8")
    dependencies = recipe["dependencies"]["runtime"]["targets"]["ubuntu"]
    (observed / "package-info.txt").write_text(
        "Depends: " + ", ".join(dependencies) + "\n", encoding="utf-8"
    )
    (observed / "installed-packages.tsv").write_text(
        "".join(
            f"{receipt.relation_name(str(item))}\t1.0\tamd64\n" for item in dependencies
        ),
        encoding="utf-8",
    )
    (observed / "installed-schemas.txt").write_text(
        "\n".join(sorted(receipt.TUNA_SCHEMAS)) + "\n", encoding="utf-8"
    )
    (observed / "canberra-command.txt").write_text("/usr/bin/canberra-gtk-play\n", encoding="utf-8")
    (observed / "sound-files.txt").write_text(
        "/usr/share/sounds/freedesktop/stereo/bell.oga\n"
        "/usr/share/sounds/freedesktop/stereo/audio-volume-change.oga\n",
        encoding="utf-8",
    )
    return provenance, observations


def check(evidence: tuple[Path, Path], **overrides):
    arguments = dict(
        root=ROOT,
        provenance=evidence[0],
        observations=evidence[1],
        package="tuna-desktop",
        distro="ubuntu",
        arch="amd64",
        revision=REVISION,
        run_url="https://github.com/tuna-os/tunaos-packages/actions/runs/123",
        run_attempt=1,
        served_url="https://repo.tunaos.org/tideforge/ubuntu/",
    )
    arguments.update(overrides)
    return receipt.receipt(**arguments)


def test_exact_served_bytes_and_installed_closure_form_a_receipt(evidence):
    value = check(evidence)
    assert value["status"] == "pass"
    assert value["package"]["sha256"] == "a" * 64
    assert value["build_inputs"]["target"]["id"] == "ubuntu"
    assert value["signature"]["primary_fingerprint"] == receipt.SIGNER


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", "main"),
        ("run_url", "https://example.invalid/run/1"),
        ("served_url", "https://repo.tunaos.org/tideforge/debian-sid/"),
        ("arch", "arm64"),
    ],
)
def test_wrong_publisher_or_target_identity_cannot_pass(evidence, field, value):
    with pytest.raises(ValueError):
        check(evidence, **{field: value})


def test_wrong_package_version_cannot_pass(evidence):
    path = evidence[1] / "tuna-desktop/installed.tsv"
    fields = path.read_text(encoding="utf-8").strip().split("\t")
    fields[1] = "9.9.9-1"
    fields[3] = "tuna-desktop_9.9.9-1_amd64.deb"
    path.write_text("\t".join(fields) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="package identity"):
        check(evidence)


@pytest.mark.parametrize("field,value", [("action_key", "sha256:" + "e" * 64), ("artifacts", [])])
def test_mismatched_action_result_cannot_pass(evidence, field, value):
    path = evidence[0] / "action-result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    result[field] = value
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        check(evidence)


@pytest.mark.parametrize("field,value", [(4, "f" * 64), (5, "999")])
def test_served_bytes_or_size_mismatch_cannot_pass(evidence, field, value):
    path = evidence[1] / "tuna-desktop/installed.tsv"
    fields = path.read_text(encoding="utf-8").strip().split("\t")
    fields[field] = value
    path.write_text("\t".join(fields) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        check(evidence)


@pytest.mark.parametrize("name", ["installed-payload.tsv", "package-info.txt", "installed-packages.tsv"])
def test_missing_payload_or_dependency_observation_cannot_pass(evidence, name):
    (evidence[1] / "tuna-desktop" / name).unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        check(evidence)


def test_missing_runtime_dependency_cannot_pass(evidence):
    path = evidence[1] / "tuna-desktop/package-info.txt"
    path.write_text(path.read_text(encoding="utf-8").replace("libcanberra-pulse, ", ""), encoding="utf-8")
    with pytest.raises(ValueError, match="runtime dependencies"):
        check(evidence)


def test_missing_schema_cannot_pass(evidence):
    path = evidence[1] / "tuna-desktop/installed-schemas.txt"
    path.write_text(path.read_text(encoding="utf-8").replace("org.gnome.desktop.sound\n", ""), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        check(evidence)


def test_unknown_or_expired_signer_cannot_pass(evidence):
    (evidence[1] / "signature-status.txt").write_text(
        "[GNUPG:] VALIDSIG " + "e" * 40 + "\n[GNUPG:] EXPKEYSIG expired\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        check(evidence)


def test_payload_from_another_archive_cannot_pass(evidence):
    path = evidence[1] / "tuna-desktop/installed-payload.tsv"
    path.write_text(path.read_text(encoding="utf-8").replace("archive\t" + "a" * 64, "archive\t" + "b" * 64), encoding="utf-8")
    with pytest.raises(ValueError, match="bound"):
        check(evidence)


def test_recipe_tree_change_cannot_reuse_old_action_inputs(evidence):
    path = evidence[0] / "action-inputs.json"
    identity = json.loads(path.read_text(encoding="utf-8"))
    identity["inputs"]["recipe"]["tree"] = "sha256:" + "e" * 64
    identity["action_key"] = receipt.cache.action_key(identity["inputs"])
    path.write_text(json.dumps(identity), encoding="utf-8")
    with pytest.raises(ValueError, match="current recipe"):
        check(evidence)


def test_cli_recheck_removes_stale_passing_receipt(evidence, tmp_path):
    output = tmp_path / "verdict.json"
    output.write_text('{"status":"pass"}\n', encoding="utf-8")
    (evidence[1] / "tuna-desktop/installed.tsv").unlink()
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
            "--package",
            "tuna-desktop",
            "--distro",
            "ubuntu",
            "--arch",
            "amd64",
            "--revision",
            REVISION,
            "--run-url",
            "https://github.com/tuna-os/tunaos-packages/actions/runs/123",
            "--served-url",
            "https://repo.tunaos.org/tideforge/ubuntu/",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not output.exists()
