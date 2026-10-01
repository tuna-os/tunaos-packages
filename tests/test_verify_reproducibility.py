"""Unit tests for scripts/verify-reproducibility.py (tunaos-packages#486)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "verify_reproducibility", ROOT / "scripts" / "verify-reproducibility.py"
)
vr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vr)


def test_sampling_covers_all_active_coordinates():
    sampled = vr.sample_cells(ROOT, sample_size=1)
    assert len(sampled) >= 5
    coordinates = {(c["engine"], c["format"], c["architecture"]) for c in sampled}
    assert ("build-chain", "rpm", "x86_64") in coordinates
    assert ("build-chain", "rpm", "aarch64") in coordinates
    assert ("tideforge", "deb", "amd64") in coordinates
    assert ("tideforge", "rpm", "x86_64") in coordinates
    assert ("tideforge", "pkg.tar.zst", "x86_64") in coordinates


def test_sampling_filters_by_engine_and_format():
    deb_sampled = vr.sample_cells(ROOT, sample_size=2, format_name="deb")
    assert deb_sampled
    assert all(c["format"] == "deb" for c in deb_sampled)

    native_sampled = vr.sample_cells(ROOT, sample_size=2, engine="build-chain")
    assert native_sampled
    assert all(c["engine"] == "build-chain" for c in native_sampled)


def test_artifact_comparison_exact_match(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    (dir_a / "package-1.0.rpm").write_bytes(b"exact binary payload")
    (dir_b / "package-1.0.rpm").write_bytes(b"exact binary payload")

    res = vr.compare_two_dirs(dir_a, dir_b, action_key="sha256:" + "a" * 64)
    assert res["reproducible"] is True
    assert res["matching"] == ["package-1.0.rpm"]
    assert res["mismatches"] == []
    assert res["missing"] == []
    assert res["extra"] == []


def test_artifact_comparison_digest_mismatch(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    (dir_a / "package-1.0.rpm").write_bytes(b"build A with timestamp 100")
    (dir_b / "package-1.0.rpm").write_bytes(b"build B with timestamp 200")

    res = vr.compare_two_dirs(dir_a, dir_b, action_key="sha256:" + "a" * 64)
    assert res["reproducible"] is False
    assert len(res["mismatches"]) == 1
    assert res["mismatches"][0]["name"] == "package-1.0.rpm"
    assert res["mismatches"][0]["expected_digest"] != res["mismatches"][0]["actual_digest"]


def test_artifact_comparison_missing_or_extra_files(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    (dir_a / "package-1.0.rpm").write_bytes(b"main")
    (dir_a / "package-devel-1.0.rpm").write_bytes(b"devel")
    (dir_b / "package-1.0.rpm").write_bytes(b"main")

    res = vr.compare_two_dirs(dir_a, dir_b)
    assert res["reproducible"] is False
    assert "package-devel-1.0.rpm" in res["missing"]


def test_compare_against_stored_action_result(tmp_path):
    rebuilt = tmp_path / "rebuilt"
    rebuilt.mkdir()
    (rebuilt / "demo.deb").write_bytes(b"rebuilt deb")

    digest = vr.action_cache.digest_file(rebuilt / "demo.deb")
    key = "sha256:" + "1" * 64

    action_result = {
        "schema": 1,
        "action_key": key,
        "artifacts": [{"name": "demo.deb", "size": len(b"rebuilt deb"), "digest": digest}],
    }

    match_verdict = vr.compare_against_result(action_result, rebuilt, expected_action_key=key)
    assert match_verdict["reproducible"] is True
    assert match_verdict["matching"] == ["demo.deb"]

    # Now tamper with the rebuilt artifact
    (rebuilt / "demo.deb").write_bytes(b"modified deb")
    mismatch_verdict = vr.compare_against_result(action_result, rebuilt, expected_action_key=key)
    assert mismatch_verdict["reproducible"] is False
    assert len(mismatch_verdict["mismatches"]) == 1


def test_quarantine_lifecycle(tmp_path):
    quar_file = tmp_path / "quarantine.yaml"
    quar = vr.load_quarantine(quar_file)
    assert quar["quarantined_packages"] == []
    assert not vr.is_quarantined(quar, "problematic-pkg", "el10", "x86_64")

    # Add to quarantine
    vr.add_to_quarantine(
        quar,
        package="problematic-pkg",
        target="el10",
        arch="x86_64",
        engine="tideforge",
        format_name="rpm",
        reason="Embedded build host in binary",
        divergent_artifacts=[{"name": "demo.rpm", "expected": "a", "actual": "b"}],
    )
    vr.save_yaml(quar, quar_file)

    loaded = vr.load_quarantine(quar_file)
    assert vr.is_quarantined(loaded, "problematic-pkg", "el10", "x86_64")
    assert not vr.is_quarantined(loaded, "problematic-pkg", "debian", "amd64")
    assert not vr.is_quarantined(loaded, "other-pkg", "el10", "x86_64")

    # Remove from quarantine
    removed = vr.remove_from_quarantine(loaded, "problematic-pkg", "el10", "x86_64")
    assert removed is True
    assert not vr.is_quarantined(loaded, "problematic-pkg", "el10", "x86_64")


def test_policy_report_computation():
    results = [
        {"engine": "tideforge", "format": "deb", "architecture": "amd64", "reproducible": True},
        {"engine": "tideforge", "format": "deb", "architecture": "amd64", "reproducible": True},
        {"engine": "tideforge", "format": "rpm", "architecture": "x86_64", "reproducible": True},
        {"engine": "tideforge", "format": "rpm", "architecture": "x86_64", "reproducible": False},
        {"engine": "build-chain", "format": "rpm", "architecture": "x86_64", "reproducible": True},
    ]
    quar = {
        "schema": 1,
        "quarantined_packages": [
            {
                "package": "divergent-pkg",
                "target": "el10",
                "architecture": "x86_64",
                "reason": "digest mismatch",
            }
        ],
    }

    report = vr.compute_policy_report(results, quarantine=quar)
    assert report["summary"]["sampled"] == 5
    assert report["summary"]["reproducible"] == 4
    assert report["summary"]["divergent"] == 1
    assert report["summary"]["quarantined"] == 1
    assert report["summary"]["reproducibility_rate"] == 80.0

    assert report["by_engine"]["tideforge"]["rate"] == 75.0
    assert report["by_engine"]["build-chain"]["rate"] == 100.0

    assert report["by_format"]["deb"]["rate"] == 100.0
    assert report["by_format"]["rpm"]["rate"] == 66.7

    md = vr.render_policy_markdown(report)
    assert "80.0%" in md
    assert "divergent-pkg" in md
    assert "| tideforge (amd64) | deb | 2 | 2 | 0 | 100.0% |" in md


def test_cli_sample_and_collect(tmp_path):
    out_file = tmp_path / "sampled.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify-reproducibility.py"),
            "sample",
            "--sample-size",
            "1",
            "--json",
        ],
        check=True,
        stdout=out_file.open("w"),
    )
    sampled = json.loads(out_file.read_text())
    assert len(sampled) >= 5

    # Test collect
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    res1 = {"cell_id": "c1", "engine": "tideforge", "format": "deb", "architecture": "amd64", "reproducible": True}
    res2 = {"cell_id": "c2", "engine": "tideforge", "format": "deb", "architecture": "amd64", "reproducible": False}
    (results_dir / "res1.json").write_text(json.dumps(res1))
    (results_dir / "res2.json").write_text(json.dumps(res2))

    status_out = tmp_path / "repro-status.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify-reproducibility.py"),
            "collect",
            "--results-dir",
            str(results_dir),
            "--out-json",
            str(status_out),
        ],
        check=True,
    )
    status_data = json.loads(status_out.read_text())
    assert status_data["summary"]["sampled"] == 2
    assert status_data["summary"]["reproducibility_rate"] == 50.0
