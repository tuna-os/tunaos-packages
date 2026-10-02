from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cache", ROOT / "scripts" / "tideforge-action-cache.py"
)
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


def test_blob_and_lease_paths_are_canonical():
    digest = "sha256:" + "a" * 64
    key = "sha256:" + "b" * 64
    assert cache.blob_path(digest) == "blobs/sha256/" + "a" * 64
    assert cache.lease_path(key) == "leases/sha256/" + "b" * 64 + ".json"


def test_lease_lifecycle_and_expiration(tmp_path: pathlib.Path):
    key = "sha256:" + "c" * 64
    lease_dir = tmp_path / "leases_root"

    # 1. Acquire lease
    lease = cache.acquire_lease(key, "worker-1", ttl_seconds=60, lease_dir=lease_dir, now=1000)
    assert lease["holder"] == "worker-1"
    assert lease["acquired_at"] == 1000
    assert lease["expires_at"] == 1060
    assert (lease_dir / cache.lease_path(key)).is_file()

    # 2. Check lease is valid
    assert cache.check_lease(key, "worker-1", lease_dir=lease_dir, now=1050) is True

    # 3. Active lease refuses another worker
    with pytest.raises(SystemExit, match="actively leased"):
        cache.acquire_lease(key, "worker-2", ttl_seconds=60, lease_dir=lease_dir, now=1050)

    # 4. Same worker can re-acquire/extend
    extended = cache.acquire_lease(key, "worker-1", ttl_seconds=120, lease_dir=lease_dir, now=1050)
    assert extended["expires_at"] == 1170

    # 5. Renew lease
    renewed = cache.renew_lease(key, "worker-1", ttl_seconds=60, lease_dir=lease_dir, now=1100)
    assert renewed["expires_at"] == 1160

    # 6. Check expired lease fails
    with pytest.raises(SystemExit, match="expired"):
        cache.check_lease(key, "worker-1", lease_dir=lease_dir, now=1200)

    # 7. Another worker can acquire expired lease
    takeover = cache.acquire_lease(key, "worker-2", ttl_seconds=60, lease_dir=lease_dir, now=1200)
    assert takeover["holder"] == "worker-2"

    # 8. Release lease
    cache.release_lease(key, "worker-2", lease_dir=lease_dir)
    assert not (lease_dir / cache.lease_path(key)).exists()


def test_cas_promotion_atomic_ordering_and_restore(tmp_path: pathlib.Path):
    artifacts_src = tmp_path / "built_artifacts"
    artifacts_src.mkdir()
    pkg1 = artifacts_src / "pkg1.rpm"
    pkg1.write_bytes(b"rpm package content")
    pkg2 = artifacts_src / "pkg2.deb"
    pkg2.write_bytes(b"deb package content")

    key = "sha256:" + "d" * 64
    result = cache.create_result(key, [pkg1, pkg2])
    res_file = tmp_path / "action-result.json"
    res_file.write_text(json.dumps(result), encoding="utf-8")

    cas_dir = tmp_path / "cas"

    # Acquire lease before promotion
    cache.acquire_lease(key, "worker-main", ttl_seconds=120, lease_dir=cas_dir, now=1000)

    # Promote to CAS
    outcome = cache.promote_to_cas(
        result,
        artifacts_src,
        cas_dir,
        holder="worker-main",
        check_lease_active=True,
        now=1050,
    )
    assert outcome["status"] == "promoted"
    assert outcome["action_key"] == key

    # Verify blobs exist in CAS
    for art in result["artifacts"]:
        blob_file = cas_dir / cache.blob_path(art["digest"])
        assert blob_file.is_file()
        assert blob_file.stat().st_size == art["size"]
        assert cache.digest_file(blob_file) == art["digest"]

    # Verify ActionResult exists in CAS
    res_in_cas = cas_dir / cache.result_path(key)
    assert res_in_cas.is_file()
    cas_res = json.loads(res_in_cas.read_text(encoding="utf-8"))
    assert cas_res["action_key"] == key

    # Verify lease was released on successful promotion
    assert not (cas_dir / cache.lease_path(key)).exists()

    # Now restore from CAS into a new directory
    out_dir = tmp_path / "restored_out"
    restored = cache.restore_from_cas(key, cas_dir, out_dir, expected_action_key=key)
    assert restored["action_key"] == key
    assert (out_dir / "artifacts" / "pkg1.rpm").read_bytes() == b"rpm package content"
    assert (out_dir / "artifacts" / "pkg2.deb").read_bytes() == b"deb package content"
    assert (out_dir / "action-result.json").is_file()


def test_cas_restore_detects_corrupted_blob(tmp_path: pathlib.Path):
    artifacts_src = tmp_path / "artifacts"
    artifacts_src.mkdir()
    pkg = artifacts_src / "demo.rpm"
    pkg.write_bytes(b"valid content")

    key = "sha256:" + "e" * 64
    result = cache.create_result(key, [pkg])
    cas_dir = tmp_path / "cas"
    cache.promote_to_cas(result, artifacts_src, cas_dir)

    # Corrupt the blob in CAS
    digest = result["artifacts"][0]["digest"]
    blob_file = cas_dir / cache.blob_path(digest)
    blob_file.write_bytes(b"tampered content")

    out_dir = tmp_path / "out"
    with pytest.raises(SystemExit, match="corrupted blob"):
        cache.restore_from_cas(key, cas_dir, out_dir)


def test_cli_subcommands(tmp_path: pathlib.Path):
    script = ROOT / "scripts" / "tideforge-action-cache.py"
    key = "sha256:" + "f" * 64
    digest = "sha256:" + "1" * 64

    # 1. blob-path
    res = subprocess.run(
        [sys.executable, str(script), "blob-path", "--digest", digest],
        capture_output=True, text=True, check=True
    )
    assert res.stdout.strip() == "blobs/sha256/" + "1" * 64

    # 2. lease-path
    res = subprocess.run(
        [sys.executable, str(script), "lease-path", "--action-key", key],
        capture_output=True, text=True, check=True
    )
    assert res.stdout.strip() == "leases/sha256/" + "f" * 64 + ".json"

    # 3. acquire, check, renew, release lease via CLI
    lease_dir = tmp_path / "leases"
    res = subprocess.run(
        [
            sys.executable, str(script), "acquire-lease",
            "--action-key", key, "--holder", "cli-worker", "--ttl", "1800",
            "--lease-dir", str(lease_dir)
        ],
        capture_output=True, text=True, check=True
    )
    lease_data = json.loads(res.stdout)
    assert lease_data["holder"] == "cli-worker"

    res = subprocess.run(
        [
            sys.executable, str(script), "check-lease",
            "--action-key", key, "--holder", "cli-worker",
            "--lease-dir", str(lease_dir)
        ],
        capture_output=True, text=True, check=True
    )
    assert "lease active" in res.stdout

    res = subprocess.run(
        [
            sys.executable, str(script), "renew-lease",
            "--action-key", key, "--holder", "cli-worker", "--ttl", "3600",
            "--lease-dir", str(lease_dir)
        ],
        capture_output=True, text=True, check=True
    )
    assert json.loads(res.stdout)["ttl_seconds"] == 3600

    res = subprocess.run(
        [
            sys.executable, str(script), "release-lease",
            "--action-key", key, "--holder", "cli-worker",
            "--lease-dir", str(lease_dir)
        ],
        capture_output=True, text=True, check=True
    )
    assert "released lease" in res.stdout


def test_package_factory_cell_has_cas_promotion_step():
    import yaml
    cell_workflow = ROOT / ".github" / "workflows" / "package-factory-cell.yml"
    assert cell_workflow.is_file()
    data = yaml.safe_load(cell_workflow.read_text(encoding="utf-8"))
    build_steps = data["jobs"]["build"]["steps"]
    step_names = [s.get("name", "") for s in build_steps]
    assert any("Promote validated ActionResult and blobs to authoritative R2 CAS" in name for name in step_names)
    step = next(s for s in build_steps if "Promote validated ActionResult and blobs to authoritative R2 CAS" in s.get("name", ""))
    assert "github.event_name == 'push'" in step.get("if", "")
    assert "github.ref == 'refs/heads/main'" in step.get("if", "")


def test_promote_to_r2_cas_script_is_executable():
    script = ROOT / "scripts" / "promote-to-r2-cas.sh"
    assert script.is_file()
    assert (script.stat().st_mode & 0o111) != 0, "promote-to-r2-cas.sh must be executable"
    res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert res.returncode == 0, f"syntax error in promote-to-r2-cas.sh: {res.stderr}"


def test_docs_and_roadmap_consistency():
    cache_doc = (ROOT / "docs" / "TIDEFORGE-ACTION-CACHE.md").read_text(encoding="utf-8")
    assert "Authoritative CAS promotion and lease contract" in cache_doc
    assert "actions/sha256/<action-key>.json" in cache_doc
    assert "blobs/sha256/<artifact-digest>" in cache_doc
    assert "leases/sha256/<action-key>.json" in cache_doc

    roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    assert "Promotion behind the factory boundary" in roadmap
    assert "Done" in roadmap or "✅" in roadmap

