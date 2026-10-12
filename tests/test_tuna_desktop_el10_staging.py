"""Candidate input admission; no build, repository, or publication claims."""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location(
    "stager", Path(__file__).parents[1] / "scripts/stage-tuna-desktop-el10.py"
)
stager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stager)


def fixture(tmp_path):
    recipe = yaml.safe_load(
        (Path(__file__).parents[1] / "packages/tuna-desktop/package.yaml").read_text()
    )
    recipe["install"]["files"] += [
        {"source": p, "destination": dest} for p, dest in stager.PORTAL_DESTINATIONS.items()
    ]
    recipe["install"]["directories"] = [
        {"source": stager.ICON_ROOT, "destination": "usr/share/tuna-desktop/icons"}
    ]
    recipe["files"]["common"] += [
        "usr/share/tuna-desktop/icons",
        *stager.PORTAL_DESTINATIONS.values(),
    ]
    rp = tmp_path / "recipe.yaml"
    rp.write_text(yaml.safe_dump(recipe))
    sha, lock = "a" * 40, b"version = 4\n"
    sp, vp = tmp_path / "source.tar.gz", tmp_path / "vendor.tar.gz"
    payload = {entry["source"]: b"fixture" for entry in recipe["install"]["files"]}
    payload.update(
        {
            stager.ICON_ROOT + "/Adwaita/" + p: b"fixture"
            for p in [
                "LICENSE-MIT",
                "UPSTREAM-COPYING",
                "index.theme",
                "cursors_scalable/default/metadata.json",
                "cursors_scalable/default/default.svg",
            ]
        }
    )
    payload["Cargo.lock"] = lock
    with tarfile.open(sp, "w:gz") as archive:
        for name, content in payload.items():
            member = tarfile.TarInfo(f"tuna-desktop-{sha}/{name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    vp.write_bytes(b"hash-bound vendor fixture")
    factory = {"targets": {}, "consumer_adapters": {}, "dependency_catalog": {}}
    cells = []
    for consumer, target in stager.CELLS.items():
        pins = {
            arch: "quay.io/example/" + target + "@sha256:" + ("1" if arch == "x86_64" else "2") * 64
            for arch in ("x86_64", "aarch64")
        }
        factory["targets"][target] = {
            "status": "supported",
            "format": "rpm",
            "architectures": ["x86_64", "aarch64"],
            "probe_images": pins,
        }
        factory["consumer_adapters"][consumer] = {
            "target": target,
            "architectures": {"linux/amd64/v2": "x86_64", "linux/arm64": "aarch64"},
            "cpuBaselines": ["armv8-a", "x86-64-v3" if consumer == "skipjack" else "x86-64-v2"],
        }
        for cap in recipe["dependencies"]["build"]["capabilities"]:
            factory["dependency_catalog"].setdefault(cap, {})[target] = ["native-" + cap]
        for platform, arch in factory["consumer_adapters"][consumer]["architectures"].items():
            cells.append(
                {
                    "consumer": consumer,
                    "target": target,
                    "arch": arch,
                    "platform": platform,
                    "base_image": pins[arch],
                    "base_reference": pins[arch],
                }
            )
    fp, ip = tmp_path / "factory.yaml", tmp_path / "inputs.json"
    fp.write_text(yaml.safe_dump(factory))
    inputs = {
        "source_sha": sha,
        "source_archive_sha256": stager.digest(sp),
        "vendor_archive_sha256": stager.digest(vp),
        "lock_sha256": hashlib.sha256(lock).hexdigest(),
        "cells": cells,
        "vendor_url": "https://github.com/tuna-os/tunaos-packages/releases/download/candidate-abc/tuna-desktop-vendor.tar.gz",
    }
    inputs["vendor_receipt"] = {
        "source_sha": sha,
        "lock_sha256": inputs["lock_sha256"],
        "archive_sha256": stager.digest(vp),
        "offline_metadata_success": True,
        "run_id": 123,
    }
    ip.write_text(json.dumps(inputs))
    return rp, fp, ip, sp, vp, tmp_path / "stage"


def test_six_native_candidates_preserve_production_pin(tmp_path):
    args = fixture(tmp_path)
    before = args[0].read_bytes()
    receipt = stager.stage(*args)
    assert args[0].read_bytes() == before
    assert len(receipt["cells"]) == 6
    assert (
        receipt["candidate_only"] and not receipt["release_qualified"] and not receipt["published"]
    )
    assert all(c["dependency_status"] == "unmeasured" for c in receipt["cells"])
    for cell in receipt["cells"]:
        candidate = yaml.safe_load(
            (
                args[-1] / f"{cell['consumer']}-{cell['target']}-{cell['arch']}" / "package.yaml"
            ).read_text()
        )
        assert candidate["targets"] == [cell["target"]]
        assert "pkgconfig(gtk4-layer-shell-0) >= 1.1" in cell["build_requires"]
        assert "pkgconfig(gtk-layer-shell-0)" not in cell["build_requires"]
        assert "--locked --offline" in candidate["build"]["commands"][-1]


@pytest.mark.parametrize(
    "change", ["source", "vendor", "lock", "vendor_receipt", "cell", "base", "platform"]
)
def test_reject_mismatched_inputs_before_staging(tmp_path, change):
    args = fixture(tmp_path)
    inputs = json.loads(args[2].read_text())
    if change in {"source", "vendor", "lock"}:
        key = {
            "source": "source_archive_sha256",
            "vendor": "vendor_archive_sha256",
            "lock": "lock_sha256",
        }[change]
        inputs[key] = "0" * 64
    elif change == "vendor_receipt":
        inputs["vendor_receipt"]["source_sha"] = "b" * 40
    elif change == "cell":
        inputs["cells"][-1] = inputs["cells"][0]
    elif change == "base":
        inputs["cells"][0]["base_image"] = inputs["cells"][1]["base_image"]
    else:
        inputs["cells"][0]["platform"] = "linux/amd64"
    args[2].write_text(json.dumps(inputs))
    with pytest.raises(ValueError):
        stager.stage(*args)
    assert not args[-1].exists()


@pytest.mark.parametrize("missing", ["target", "capability", "portal", "icons", "ownership", "cpu"])
def test_missing_prerequisite_is_blocking(tmp_path, missing):
    args = fixture(tmp_path)
    recipe = yaml.safe_load(args[0].read_text())
    factory = yaml.safe_load(args[1].read_text())
    if missing == "target":
        del factory["targets"]["alma10"]
    elif missing == "capability":
        del factory["dependency_catalog"]["rust"]["alma10"]
    elif missing == "portal":
        recipe["install"]["files"] = [
            entry
            for entry in recipe["install"]["files"]
            if entry["source"] not in stager.PORTAL_FILES
        ]
    elif missing == "icons":
        recipe["install"]["directories"] = []
    elif missing == "ownership":
        recipe["files"]["common"].remove("usr/bin/tuna-greeter")
    else:
        factory["consumer_adapters"]["albacore"]["cpuBaselines"] = ["x86-64-v3", "armv8-a"]
    args[0].write_text(yaml.safe_dump(recipe))
    args[1].write_text(yaml.safe_dump(factory))
    with pytest.raises(ValueError):
        stager.stage(*args)
    assert not args[-1].exists()


def test_existing_output_not_reused(tmp_path):
    args = fixture(tmp_path)
    args[-1].mkdir()
    with pytest.raises(ValueError, match="stale staging"):
        stager.stage(*args)


def test_source_archive_payload_and_path_are_checked(tmp_path):
    args = fixture(tmp_path)
    with tarfile.open(args[3], "w:gz") as archive:
        member = tarfile.TarInfo("../Cargo.lock")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"lock"))
    inputs = json.loads(args[2].read_text())
    inputs["source_archive_sha256"] = stager.digest(args[3])
    args[2].write_text(json.dumps(inputs))
    with pytest.raises(ValueError, match="unsafe source archive"):
        stager.stage(*args)
    assert not args[-1].exists()


def test_wrong_sha_cannot_relabel_archive(tmp_path):
    args = fixture(tmp_path)
    inputs = json.loads(args[2].read_text())
    inputs["source_sha"] = "b" * 40
    inputs["vendor_receipt"]["source_sha"] = "b" * 40
    args[2].write_text(json.dumps(inputs))
    with pytest.raises(ValueError, match="source missing"):
        stager.stage(*args)
    assert not args[-1].exists()
