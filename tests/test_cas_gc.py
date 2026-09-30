"""Tests for CAS reachability and garbage collection mark-and-sweep engine (report-only).

Validates #430 step 5 / #485:
- Mark phase: retained ActionResults + repo metadata mark reachable blobs.
- Sweep phase: unreachable blobs evaluated against age grace period.
- Grace vs. candidate classification.
- Tombstone manifest generation.
- Last-referencing ActionResult attribution (and orphan detection).
- Report-only safety (no file deletion).
- Inventory loaders and Markdown rendering.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cas_gc


def create_blob(cas_dir: pathlib.Path, hex_hash: str, size: int = 1024, mtime: float | None = None) -> pathlib.Path:
    blob_dir = cas_dir / "blobs" / "sha256"
    blob_dir.mkdir(parents=True, exist_ok=True)
    blob_file = blob_dir / hex_hash
    blob_file.write_bytes(b"x" * size)
    if mtime is not None:
        import os
        os.utime(blob_file, (mtime, mtime))
    return blob_file


def create_action(
    cas_dir: pathlib.Path,
    action_hash: str,
    artifacts: list[dict],
    mtime: float | None = None,
    created_at: str | None = None,
) -> pathlib.Path:
    act_dir = cas_dir / "actions" / "sha256"
    act_dir.mkdir(parents=True, exist_ok=True)
    act_file = act_dir / f"{action_hash}.json"
    data = {
        "schema": 1,
        "action_key": f"sha256:{action_hash}",
        "artifacts": artifacts,
    }
    if created_at:
        data["created_at"] = created_at
    act_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(act_file, (mtime, mtime))
    return act_file


def test_empty_cas_returns_clean_report(tmp_path):
    blobs, actions = cas_gc.scan_local_cas(tmp_path)
    assert len(blobs) == 0
    assert len(actions) == 0

    reachable, attribution = cas_gc.compute_reachability(actions.values())
    report = cas_gc.run_mark_and_sweep(blobs, reachable, attribution)

    assert report["schema"] == 1
    assert report["mode"] == "report-only"
    assert report["summary"]["total_blobs"] == 0
    assert report["summary"]["reachable_blobs"] == 0
    assert report["summary"]["unreachable_blobs"] == 0
    assert report["summary"]["candidate_blobs"] == 0
    assert len(report["unreachable_blobs"]) == 0
    assert len(report["tombstones"]) == 0


def test_retained_action_results_mark_all_referenced_blobs(tmp_path):
    h1 = "1" * 64
    h2 = "2" * 64
    act_h = "a" * 64

    create_blob(tmp_path, h1, size=2048)
    create_blob(tmp_path, h2, size=4096)
    create_action(
        tmp_path,
        act_h,
        artifacts=[
            {"name": "pkg1.rpm", "size": 2048, "digest": f"sha256:{h1}"},
            {"name": "pkg2.rpm", "size": 4096, "digest": f"sha256:{h2}"},
        ],
    )

    blobs, actions = cas_gc.scan_local_cas(tmp_path)
    assert len(blobs) == 2
    assert len(actions) == 1

    reachable, attribution = cas_gc.compute_reachability(actions.values())
    assert f"sha256:{h1}" in reachable
    assert f"sha256:{h2}" in reachable

    report = cas_gc.run_mark_and_sweep(blobs, reachable, attribution)
    assert report["summary"]["total_blobs"] == 2
    assert report["summary"]["reachable_blobs"] == 2
    assert report["summary"]["unreachable_blobs"] == 0
    assert report["summary"]["candidate_blobs"] == 0
    assert report["summary"]["reachable_bytes"] == 6144
    assert len(report["tombstones"]) == 0


def test_unreachable_blob_in_grace_period_is_classified_as_grace(tmp_path):
    # Reference evaluation time: 2026-09-01T00:00:00Z (timestamp 1788220800)
    as_of = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    # Blob created 2 days before as_of (2 * 86400 = 172800 s ago)
    blob_time = as_of.timestamp() - (2 * 86400)

    h_reach = "1" * 64
    h_unreach = "2" * 64
    act_h = "a" * 64

    create_blob(tmp_path, h_reach, size=1000, mtime=blob_time)
    create_blob(tmp_path, h_unreach, size=2000, mtime=blob_time)
    create_action(
        tmp_path,
        act_h,
        artifacts=[{"name": "kept.rpm", "size": 1000, "digest": f"sha256:{h_reach}"}],
        mtime=blob_time,
    )

    blobs, actions = cas_gc.scan_local_cas(tmp_path)
    reachable, attribution = cas_gc.compute_reachability(actions.values())

    # Grace period: 7 days
    report = cas_gc.run_mark_and_sweep(
        blobs,
        reachable,
        attribution,
        grace_period_seconds=7 * 86400,
        as_of=as_of,
    )

    assert report["summary"]["total_blobs"] == 2
    assert report["summary"]["reachable_blobs"] == 1
    assert report["summary"]["unreachable_blobs"] == 1
    assert report["summary"]["grace_blobs"] == 1
    assert report["summary"]["candidate_blobs"] == 0
    assert report["summary"]["candidate_bytes"] == 0

    unreach_entry = report["unreachable_blobs"][0]
    assert unreach_entry["digest"] == f"sha256:{h_unreach}"
    assert unreach_entry["status"] == "grace"
    assert unreach_entry["age_human"] == "2d 0h"
    assert len(report["tombstones"]) == 0


def test_unreachable_blob_past_grace_period_is_candidate_and_generates_tombstone(tmp_path):
    as_of = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    # Blob created 10 days before as_of (10 * 86400 = 864000 s ago)
    blob_time = as_of.timestamp() - (10 * 86400)

    h_orphan = "3" * 64
    create_blob(tmp_path, h_orphan, size=5000, mtime=blob_time)

    blobs, actions = cas_gc.scan_local_cas(tmp_path)
    reachable, attribution = cas_gc.compute_reachability(actions.values())

    report = cas_gc.run_mark_and_sweep(
        blobs,
        reachable,
        attribution,
        grace_period_seconds=7 * 86400,
        as_of=as_of,
    )

    assert report["summary"]["total_blobs"] == 1
    assert report["summary"]["unreachable_blobs"] == 1
    assert report["summary"]["grace_blobs"] == 0
    assert report["summary"]["candidate_blobs"] == 1
    assert report["summary"]["candidate_bytes"] == 5000

    unreach_entry = report["unreachable_blobs"][0]
    assert unreach_entry["digest"] == f"sha256:{h_orphan}"
    assert unreach_entry["status"] == "candidate"
    assert unreach_entry["age_human"] == "10d 0h"
    assert unreach_entry["last_referencing_action"] is None

    assert len(report["tombstones"]) == 1
    tb = report["tombstones"][0]
    assert tb["schema"] == 1
    assert tb["digest"] == f"sha256:{h_orphan}"
    assert tb["size"] == 5000
    assert tb["grace_period_seconds"] == 604800
    assert tb["reason"] == "unreachable_past_grace_period"


def test_last_referencing_action_result_attribution(tmp_path):
    as_of = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    t_old = as_of.timestamp() - (12 * 86400)
    t_new = as_of.timestamp() - (1 * 86400)

    # Blob 1: in retained ActionResult (reachable)
    # Blob 2: in superseded ActionResult (unreachable candidate, attributed to old ActionResult)
    # Blob 3: never in any ActionResult (unreachable candidate, orphan)
    h_kept = "1" * 64
    h_stale = "2" * 64
    h_orphan = "3" * 64

    act_stale = "e" * 64
    act_active = "a" * 64

    create_blob(tmp_path, h_kept, size=100, mtime=t_new)
    create_blob(tmp_path, h_stale, size=200, mtime=t_old)
    create_blob(tmp_path, h_orphan, size=300, mtime=t_old)

    # Active ActionResult (retained)
    create_action(
        tmp_path,
        act_active,
        artifacts=[{"name": "glib2-2.88.0-1.rpm", "size": 100, "digest": f"sha256:{h_kept}"}],
        mtime=t_new,
    )

    # Stale ActionResult (superseded / un-retained)
    stale_record = cas_gc.load_action_result({
        "schema": 1,
        "action_key": f"sha256:{act_stale}",
        "artifacts": [{"name": "glib2-2.87.3-1.rpm", "size": 200, "digest": f"sha256:{h_stale}"}],
        "created_at": "2026-08-20T00:00:00Z",
    })

    blobs, actions = cas_gc.scan_local_cas(tmp_path)
    # actions contains only active
    reachable, attribution = cas_gc.compute_reachability(
        retained_actions=actions.values(),
        all_known_actions=[stale_record],
    )

    report = cas_gc.run_mark_and_sweep(
        blobs,
        reachable,
        attribution,
        grace_period_seconds=7 * 86400,
        as_of=as_of,
    )

    assert report["summary"]["reachable_blobs"] == 1
    assert report["summary"]["candidate_blobs"] == 2

    by_digest = {u["digest"]: u for u in report["unreachable_blobs"]}
    stale_entry = by_digest[f"sha256:{h_stale}"]
    assert stale_entry["last_referencing_action"] == f"sha256:{act_stale}"
    assert stale_entry["last_referencing_artifact"] == "glib2-2.87.3-1.rpm"

    orphan_entry = by_digest[f"sha256:{h_orphan}"]
    assert orphan_entry["last_referencing_action"] is None
    assert orphan_entry["last_referencing_artifact"] is None


def test_repository_metadata_marks_packages_reachable(tmp_path):
    h_repo_pkg = "4" * 64
    create_blob(tmp_path, h_repo_pkg, size=1500)

    blobs, actions = cas_gc.scan_local_cas(tmp_path)
    assert len(actions) == 0  # No retained ActionResults

    # Repository metadata references the blob by digest
    repo_ref = cas_gc.RepoReference(
        name="foo",
        evr="1.0-1",
        arch="x86_64",
        digest=f"sha256:{h_repo_pkg}",
        location="repo/10-x86_64/foo-1.0-1.rpm",
        source="https://repo.tunaos.org/repo/10/x86_64",
    )

    reachable, attribution = cas_gc.compute_reachability(
        retained_actions=[],
        repo_references=[repo_ref],
    )
    assert f"sha256:{h_repo_pkg}" in reachable

    report = cas_gc.run_mark_and_sweep(blobs, reachable, attribution)
    assert report["summary"]["reachable_blobs"] == 1
    assert report["summary"]["unreachable_blobs"] == 0


def test_gc_is_strictly_report_only_and_does_not_modify_or_delete_blobs(tmp_path):
    h1 = "1" * 64
    h2 = "2" * 64
    f1 = create_blob(tmp_path, h1, size=100)
    f2 = create_blob(tmp_path, h2, size=200)
    act_file = create_action(
        tmp_path,
        "a" * 64,
        artifacts=[{"name": "x.rpm", "size": 100, "digest": f"sha256:{h1}"}],
    )

    blobs, actions = cas_gc.scan_local_cas(tmp_path)
    reachable, attribution = cas_gc.compute_reachability(actions.values())
    report = cas_gc.run_mark_and_sweep(
        blobs,
        reachable,
        attribution,
        grace_period_seconds=0,  # All unreachable immediately candidate
    )

    assert report["summary"]["candidate_blobs"] == 1
    # Check that ALL files still exist and content is untouched
    assert f1.is_file() and f1.stat().st_size == 100
    assert f2.is_file() and f2.stat().st_size == 200
    assert act_file.is_file()


def test_inventory_json_and_lsf_parsers():
    inv_items = [
        {"Path": "blobs/sha256/" + "1" * 64, "Size": 1024, "ModTime": "2026-08-20T12:00:00Z"},
        {
            "Path": "actions/sha256/" + "a" * 64 + ".json",
            "Size": 256,
            "ModTime": "2026-08-20T12:05:00Z",
            "Content": {
                "schema": 1,
                "action_key": "sha256:" + "a" * 64,
                "artifacts": [{"name": "test.rpm", "size": 1024, "digest": "sha256:" + "1" * 64}],
            },
        },
    ]

    blobs, actions = cas_gc.parse_inventory_json(inv_items)
    assert len(blobs) == 1
    assert "sha256:" + "1" * 64 in blobs
    assert len(actions) == 1
    assert "sha256:" + "a" * 64 in actions

    lsf_lines = [
        "2048 2026-08-21 10:00:00 blobs/sha256/" + "2" * 64,
        "4096 2026-08-21 11:00:00 blobs/sha256/" + "3" * 64,
    ]
    lsf_blobs = cas_gc.parse_inventory_lsf(lsf_lines)
    assert len(lsf_blobs) == 2
    assert "sha256:" + "2" * 64 in lsf_blobs
    assert lsf_blobs["sha256:" + "2" * 64].size == 2048


def test_markdown_report_formatting(tmp_path):
    h = "5" * 64
    create_blob(tmp_path, h, size=1048576)  # 1 MB
    blobs, actions = cas_gc.scan_local_cas(tmp_path)
    reachable, attribution = cas_gc.compute_reachability(actions.values())
    report = cas_gc.run_mark_and_sweep(
        blobs,
        reachable,
        attribution,
        grace_period_seconds=0,
        as_of=datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc),
    )

    md = cas_gc.render_markdown_report(report)
    assert "# CAS Garbage Collection & Reachability Report" in md
    assert "Mode: Report-Only" in md
    assert "| **Total CAS Blobs** | 1 | 1.00 MB |" in md
    assert "Sweep Candidates" in md
    assert "Tombstones (Sweep Candidates)" in md


def test_tombstones_directory_export(tmp_path):
    tombstones = [
        {
            "schema": 1,
            "digest": "sha256:" + "9" * 64,
            "size": 4096,
            "path": "blobs/sha256/" + "9" * 64,
            "created_at": "2026-08-15T00:00:00Z",
            "tombstone_time": "2026-09-01T00:00:00Z",
            "grace_period_seconds": 604800,
            "last_referencing_action": None,
            "last_referencing_artifact": None,
            "reason": "unreachable_past_grace_period",
        }
    ]
    out_dir = tmp_path / "output_tombstones"
    cas_gc.write_tombstones_dir(tombstones, out_dir)

    target_file = out_dir / "tombstones" / "sha256" / f"{'9' * 64}.json"
    assert target_file.is_file()
    saved = json.loads(target_file.read_text(encoding="utf-8"))
    assert saved["schema"] == 1
    assert saved["tombstone"]["digest"] == "sha256:" + "9" * 64


def test_cli_end_to_end(tmp_path):
    cas_dir = tmp_path / "cas"
    h_kept = "1" * 64
    h_stale = "2" * 64
    create_blob(cas_dir, h_kept, size=1024)
    create_blob(cas_dir, h_stale, size=2048)
    create_action(
        cas_dir,
        "a" * 64,
        artifacts=[{"name": "kept.rpm", "size": 1024, "digest": f"sha256:{h_kept}"}],
    )

    json_out = tmp_path / "report.json"
    md_out = tmp_path / "report.md"
    tb_dir = tmp_path / "tb"

    rc = cas_gc.main([
        "--cas-dir", str(cas_dir),
        "--grace-period-seconds", "0",
        "--json", str(json_out),
        "--markdown", str(md_out),
        "--tombstones-dir", str(tb_dir),
        "--summary",
    ])
    assert rc == 0
    assert json_out.is_file()
    assert md_out.is_file()
    assert (tb_dir / "tombstones" / "sha256" / f"{h_stale}.json").is_file()

    report_data = json.loads(json_out.read_text(encoding="utf-8"))
    assert report_data["summary"]["reachable_blobs"] == 1
    assert report_data["summary"]["candidate_blobs"] == 1


def test_retained_keys_filter_file(tmp_path):
    cas_dir = tmp_path / "cas"
    h1 = "1" * 64
    h2 = "2" * 64
    act1 = "a" * 64
    act2 = "b" * 64

    create_blob(cas_dir, h1, size=100)
    create_blob(cas_dir, h2, size=200)

    create_action(cas_dir, act1, artifacts=[{"name": "a.rpm", "size": 100, "digest": f"sha256:{h1}"}])
    create_action(cas_dir, act2, artifacts=[{"name": "b.rpm", "size": 200, "digest": f"sha256:{h2}"}])

    # Retained keys file only contains act1
    retained_file = tmp_path / "retained.txt"
    retained_file.write_text(f"# comment\nsha256:{act1}\n\n", encoding="utf-8")

    json_out = tmp_path / "report.json"
    rc = cas_gc.main([
        "--cas-dir", str(cas_dir),
        "--retained-keys", str(retained_file),
        "--grace-period-seconds", "0",
        "--json", str(json_out),
    ])
    assert rc == 0
    report_data = json.loads(json_out.read_text(encoding="utf-8"))
    assert report_data["roots"]["retained_action_results_count"] == 1
    assert report_data["summary"]["reachable_blobs"] == 1
    assert report_data["summary"]["candidate_blobs"] == 1

    # Check that h2 is candidate and attributed to act2
    cand = report_data["unreachable_blobs"][0]
    assert cand["digest"] == f"sha256:{h2}"
    assert cand["last_referencing_action"] == f"sha256:{act2}"


def test_helpers_formatting():
    assert cas_gc.format_bytes(0) == "0 B"
    assert cas_gc.format_bytes(1023) == "1023 B"
    assert cas_gc.format_bytes(1024) == "1.00 KB"
    assert cas_gc.format_bytes(1048576) == "1.00 MB"
    assert cas_gc.format_bytes(1073741824) == "1.00 GB"

    assert cas_gc.format_duration(None) == "unknown"
    assert cas_gc.format_duration(30) == "30s"
    assert cas_gc.format_duration(3600) == "1h 0m"
    assert cas_gc.format_duration(90000) == "1d 1h"

    assert cas_gc.parse_timestamp(None) is None
    assert cas_gc.parse_timestamp("") is None
    assert cas_gc.parse_timestamp("invalid") is None
    assert cas_gc.parse_timestamp(1700000000) == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert cas_gc.parse_timestamp("2026-09-01T12:30:00Z") == datetime(2026, 9, 1, 12, 30, 0, tzinfo=timezone.utc)

