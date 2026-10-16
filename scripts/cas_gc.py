#!/usr/bin/env python3
"""CAS reachability and garbage collection (report-only).

Implements the mark-and-sweep reachability engine for Tideforge's Content-Addressed
Storage (CAS) per #430 step 5.

The production CAS storage contract:
    actions/sha256/<action-key>.json     -- Authoritative ActionResult manifests
    blobs/sha256/<artifact-digest>       -- Immutable content blobs

Mark phase:
    1. Enumerate retained ActionResults and mark all referenced artifact digests.
    2. Enumerate active repository metadata (RPM repomd/primary, flat-APT Packages,
       pacman DB) and mark all served package digests/files.
    3. Compute the reachable blob set as the union of these roots.

Sweep phase (Report-Only):
    1. Identify all CAS blobs not in the reachable set (unreachable blobs).
    2. Evaluate each unreachable blob against an age grace period (default 7 days).
    3. Blobs younger than the grace period are classified as 'grace'.
    4. Blobs older than the grace period are classified as sweep 'candidate' and
       recorded with a formal tombstone manifest.
    5. Attributes unreachable blobs to their last-referencing ActionResult when
       historical ActionResults are known, or records them as unreferenced orphans.

POLICY: This script is strictly report-only. No deletion is performed. Destructive
sweep is a separate, gated step after the report runs cleanly.
"""
from __future__ import annotations

import argparse
import collections
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
import pathlib
import re
import sys
from typing import Any, Iterable

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

SCHEMA = 1
DEFAULT_GRACE_PERIOD_SECONDS = 7 * 86400  # 7 days in seconds
SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$", re.IGNORECASE)


def require_sha256(value: str, label: str = "digest") -> str:
    """Validate and normalize a sha256 string to 'sha256:<64-hex>'."""
    match = SHA256_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"{label} must be a 64-character lowercase hexadecimal hash, got {value!r}")
    return f"sha256:{match.group(1).lower()}"


def parse_timestamp(value: Any) -> datetime | None:
    """Parse various timestamp representations into a UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        if math.isnan(value) or value < 0:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Support numeric string timestamp
        try:
            num = float(text)
            return datetime.fromtimestamp(num, tz=timezone.utc)
        except ValueError:
            pass
        # Support ISO 8601 / RFC 3339 format
        try:
            # Replace Z with +00:00 for fromisoformat compatibility
            clean = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
        # Support rclone lsf format "YYYY-MM-DD HH:MM:SS" or with decimals
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                dt = datetime.strptime(text, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def format_bytes(num_bytes: int) -> str:
    """Format byte count into human-readable string (e.g. 1.25 MB)."""
    if num_bytes < 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(num_bytes)
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.2f} {units[unit_index]}"


def format_duration(seconds: float | int | None) -> str:
    """Format duration in seconds into human-readable string (e.g. 7d 12h or 3h 15m)."""
    if seconds is None:
        return "unknown"
    sec = max(0, int(seconds))
    days = sec // 86400
    hours = (sec % 86400) // 3600
    minutes = (sec % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{sec}s"


@dataclass(frozen=True)
class BlobRecord:
    """Representation of a CAS content blob."""
    digest: str           # "sha256:<64 hex>"
    size: int             # bytes
    path: str             # e.g. "blobs/sha256/<hash>"
    created_at: datetime | None = None
    mtime: float | None = None


@dataclass(frozen=True)
class ActionResultRecord:
    """Representation of an ActionResult manifest."""
    action_key: str       # "sha256:<64 hex>"
    path: str             # e.g. "actions/sha256/<hash>.json"
    artifacts: list[dict] # [{"name": ..., "size": ..., "digest": ...}]
    created_at: datetime | None = None
    mtime: float | None = None


@dataclass(frozen=True)
class RepoReference:
    """Package reference from served repository metadata."""
    name: str
    evr: str
    arch: str
    digest: str | None
    location: str | None
    source: str


def load_action_result(
    path_or_dict: pathlib.Path | str | dict,
    mtime: float | None = None,
    created_at: datetime | None = None,
) -> ActionResultRecord:
    """Load and validate an ActionResult manifest."""
    if isinstance(path_or_dict, (pathlib.Path, str)):
        path = pathlib.Path(path_or_dict)
        data = json.loads(path.read_text(encoding="utf-8"))
        rel_path = path.as_posix()
        if mtime is None:
            try:
                st = path.stat()
                mtime = st.st_mtime
                if created_at is None:
                    created_at = datetime.fromtimestamp(mtime, tz=timezone.utc)
            except OSError:
                pass
    else:
        data = path_or_dict
        rel_path = ""

    if not isinstance(data, dict):
        raise ValueError("ActionResult manifest must be a JSON object")

    schema = data.get("schema", SCHEMA)
    if schema != SCHEMA:
        raise ValueError(f"unsupported ActionResult schema: {schema}")

    key_raw = str(data.get("action_key", ""))
    key = require_sha256(key_raw, "action_key")

    raw_artifacts = data.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise ValueError("ActionResult must contain a non-empty artifacts list")

    parsed_artifacts = []
    for entry in raw_artifacts:
        if not isinstance(entry, dict):
            raise ValueError("malformed artifact entry in ActionResult")
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ValueError("empty artifact name in ActionResult")
        size = entry.get("size")
        if not isinstance(size, int) or size < 0:
            raise ValueError(f"invalid artifact size for {name}: {size}")
        digest = require_sha256(str(entry.get("digest", "")), f"artifact digest for {name}")
        parsed_artifacts.append({"name": name, "size": size, "digest": digest})

    if created_at is None and "created_at" in data:
        created_at = parse_timestamp(data["created_at"])
    if created_at is None and "source_date_epoch" in data:
        created_at = parse_timestamp(data["source_date_epoch"])

    if not rel_path:
        raw_hash = key.removeprefix("sha256:")
        rel_path = f"actions/sha256/{raw_hash}.json"

    return ActionResultRecord(
        action_key=key,
        path=rel_path,
        artifacts=parsed_artifacts,
        created_at=created_at,
        mtime=mtime if mtime is not None else (created_at.timestamp() if created_at else None),
    )


def scan_local_cas(
    cas_dir: pathlib.Path,
) -> tuple[dict[str, BlobRecord], dict[str, ActionResultRecord]]:
    """Scan a local directory structured as a CAS repository."""
    cas_dir = pathlib.Path(cas_dir).resolve()
    blobs: dict[str, BlobRecord] = {}
    action_results: dict[str, ActionResultRecord] = {}

    # Scan ActionResults: actions/sha256/*.json or actions/*.json
    actions_root = cas_dir / "actions"
    if actions_root.is_dir():
        for path in sorted(actions_root.rglob("*.json")):
            if not path.is_file():
                continue
            try:
                record = load_action_result(path)
                action_results[record.action_key] = record
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"warning: ignoring invalid ActionResult {path}: {exc}\n")

    # Scan Blobs: blobs/sha256/* or blobs/*
    blobs_root = cas_dir / "blobs"
    if blobs_root.is_dir():
        for path in sorted(blobs_root.rglob("*")):
            if not path.is_file() or path.suffix == ".tmp":
                continue
            name = path.name
            try:
                digest = require_sha256(name, "blob name")
            except ValueError:
                # If name isn't a hash, check if parent directory is sha256 or digest
                continue
            st = path.stat()
            rel_path = path.relative_to(cas_dir).as_posix()
            created_at = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            blobs[digest] = BlobRecord(
                digest=digest,
                size=st.st_size,
                path=rel_path,
                created_at=created_at,
                mtime=st.st_mtime,
            )

    return blobs, action_results


def parse_inventory_json(
    items: list[dict[str, Any]],
) -> tuple[dict[str, BlobRecord], dict[str, ActionResultRecord]]:
    """Parse an object inventory list (e.g. from rclone lsf --json / S3 API)."""
    blobs: dict[str, BlobRecord] = {}
    action_results: dict[str, ActionResultRecord] = {}

    for item in items:
        if item.get("IsDir", False):
            continue
        path_str = str(item.get("Path", item.get("path", item.get("Key", item.get("key", "")))))
        if not path_str:
            continue
        size = int(item.get("Size", item.get("size", 0)))
        modtime_raw = item.get("ModTime", item.get("modtime", item.get("LastModified", item.get("time"))))
        created_at = parse_timestamp(modtime_raw)
        mtime = created_at.timestamp() if created_at else None

        # Check if it's an ActionResult
        if path_str.startswith("actions/") and path_str.endswith(".json"):
            # Check if payload/content is included in item
            content = item.get("Content", item.get("content", item.get("payload", item.get("data"))))
            if isinstance(content, str):
                try:
                    content_dict = json.loads(content)
                    ar = load_action_result(content_dict, mtime=mtime, created_at=created_at)
                    action_results[ar.action_key] = ar
                    continue
                except Exception:  # noqa: BLE001
                    pass
            elif isinstance(content, dict):
                try:
                    ar = load_action_result(content, mtime=mtime, created_at=created_at)
                    action_results[ar.action_key] = ar
                    continue
                except Exception:  # noqa: BLE001
                    pass
            # If no content is inline, try deriving action key from filename
            stem = pathlib.PurePath(path_str).stem
            try:
                action_key = require_sha256(stem, "action_key filename")
                # When only inventory path is available without artifact details, record stub
                action_results[action_key] = ActionResultRecord(
                    action_key=action_key,
                    path=path_str,
                    artifacts=[],
                    created_at=created_at,
                    mtime=mtime,
                )
            except ValueError:
                pass
            continue

        # Check if it's a blob
        if path_str.startswith("blobs/"):
            blob_name = pathlib.PurePath(path_str).name
            try:
                digest = require_sha256(blob_name, "blob filename")
                blobs[digest] = BlobRecord(
                    digest=digest,
                    size=size,
                    path=path_str,
                    created_at=created_at,
                    mtime=mtime,
                )
            except ValueError:
                pass

    return blobs, action_results


def parse_inventory_lsf(lines: Iterable[str]) -> dict[str, BlobRecord]:
    """Parse rclone lsf output lines (supporting semicolon, tab, or space delimited fields)."""
    blobs: dict[str, BlobRecord] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        path_str = ""
        size = 0
        modtime_str = ""

        # Check semicolon delimited (rclone lsf default when multiple format specifiers are used)
        if ";" in line:
            parts = [p.strip() for p in line.split(";")]
            # Match parts by content
            for part in parts:
                if part.startswith("blobs/") or "/blobs/" in part:
                    path_str = part
                elif part.isdigit():
                    size = int(part)
                elif "-" in part and ":" in part:
                    modtime_str = part
        else:
            # Whitespace separated
            parts = line.split()
            if len(parts) >= 4 and parts[0].isdigit():
                # size date time path
                size = int(parts[0])
                modtime_str = f"{parts[1]} {parts[2]}"
                path_str = parts[3]
            elif len(parts) >= 3 and parts[0].isdigit():
                # size time path
                size = int(parts[0])
                modtime_str = parts[1]
                path_str = parts[2]
            elif len(parts) == 1:
                path_str = parts[0]
            else:
                for part in parts:
                    if part.startswith("blobs/"):
                        path_str = part
                    elif part.isdigit() and size == 0:
                        size = int(part)

        if path_str and (path_str.startswith("blobs/") or "/blobs/" in path_str):
            name = pathlib.PurePath(path_str).name
            try:
                digest = require_sha256(name, "blob filename")
                dt = parse_timestamp(modtime_str) if modtime_str else None
                blobs[digest] = BlobRecord(
                    digest=digest,
                    size=size,
                    path=path_str,
                    created_at=dt,
                    mtime=dt.timestamp() if dt else None,
                )
            except ValueError:
                continue
    return blobs


def scan_repo_metadata(
    factory_manifest: pathlib.Path | None = None,
    repo_urls: list[str] | None = None,
    cache_dir: pathlib.Path | None = None,
) -> list[RepoReference]:
    """Scan current repository metadata from factory manifest and/or explicit URLs."""
    refs: list[RepoReference] = []
    urls_to_scan: list[tuple[str, str]] = []  # (url, format)

    if factory_manifest and factory_manifest.is_file():
        try:
            pub_mod_path = HERE / "published_index.py"
            if pub_mod_path.is_file():
                spec = importlib.util.spec_from_file_location("published_index", pub_mod_path)
                pub_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(pub_mod)
                factory_data = pub_mod.load(factory_manifest)
            else:
                import yaml
                factory_data = yaml.safe_load(factory_manifest.read_text(encoding="utf-8")) or {}

            for target_id, target in (factory_data.get("targets") or {}).items():
                fmt = target.get("format", "rpm")
                for arch in target.get("architectures", []):
                    urls = target.get("published_index", {}).get(arch, [])
                    if isinstance(urls, str):
                        urls = [urls]
                    for url in urls:
                        if url:
                            urls_to_scan.append((url, fmt))
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"warning: error reading factory manifest {factory_manifest}: {exc}\n")

    if repo_urls:
        for u in repo_urls:
            urls_to_scan.append((u, "rpm"))

    if not urls_to_scan:
        return refs

    repo_idx_path = HERE / "repo_index.py"
    if repo_idx_path.is_file():
        try:
            spec = importlib.util.spec_from_file_location("repo_index", repo_idx_path)
            repo_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(repo_mod)
            cache = cache_dir or (ROOT / ".cache" / "cas-gc-repos")
            for url, fmt in urls_to_scan:
                try:
                    for row in repo_mod.iter_rows(url, fmt, cache):
                        digest = None
                        if "digest" in row:
                            try:
                                digest = require_sha256(row["digest"])
                            except ValueError:
                                pass
                        elif "sha256" in row:
                            try:
                                digest = require_sha256(row["sha256"])
                            except ValueError:
                                pass
                        refs.append(RepoReference(
                            name=row.get("name", ""),
                            evr=row.get("evr", ""),
                            arch=row.get("arch", ""),
                            digest=digest,
                            location=row.get("location"),
                            source=url,
                        ))
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(f"warning: failed to read repository index {url}: {exc}\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"warning: repo_index unavailable: {exc}\n")

    return refs


def compute_reachability(
    retained_actions: Iterable[ActionResultRecord],
    repo_references: Iterable[RepoReference] = (),
    all_known_actions: Iterable[ActionResultRecord] = (),
) -> tuple[set[str], dict[str, list[dict[str, Any]]]]:
    """Mark reachable blobs and build attribution mapping.

    Returns:
        (reachable_digests_set, attribution_map)
        where attribution_map maps digest -> list of referencing ActionResult info.
    """
    reachable_digests: set[str] = set()
    attribution: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)

    # 1. Mark from Retained ActionResults
    retained_keys = set()
    for ar in retained_actions:
        retained_keys.add(ar.action_key)
        for artifact in ar.artifacts:
            digest = artifact["digest"]
            reachable_digests.add(digest)
            attribution[digest].append({
                "action_key": ar.action_key,
                "artifact_name": artifact["name"],
                "path": ar.path,
                "created_at": ar.created_at.isoformat() if ar.created_at else None,
                "mtime": ar.mtime,
                "retained": True,
            })

    # 2. Mark from Repository Metadata
    for ref in repo_references:
        if ref.digest:
            reachable_digests.add(ref.digest)
        # If location href matches a blob digest or filename
        if ref.location:
            loc_stem = pathlib.PurePath(ref.location).name
            try:
                loc_digest = require_sha256(loc_stem)
                reachable_digests.add(loc_digest)
            except ValueError:
                pass

    # 3. Include historical / superseded ActionResults for attribution of unreachable blobs
    for ar in all_known_actions:
        if ar.action_key in retained_keys:
            continue
        for artifact in ar.artifacts:
            digest = artifact["digest"]
            attribution[digest].append({
                "action_key": ar.action_key,
                "artifact_name": artifact["name"],
                "path": ar.path,
                "created_at": ar.created_at.isoformat() if ar.created_at else None,
                "mtime": ar.mtime,
                "retained": False,
            })

    return reachable_digests, attribution


def run_mark_and_sweep(
    blobs: dict[str, BlobRecord],
    reachable_digests: set[str],
    attribution: dict[str, list[dict[str, Any]]],
    grace_period_seconds: int = DEFAULT_GRACE_PERIOD_SECONDS,
    as_of: datetime | None = None,
    retained_actions_count: int = 0,
    repo_sources_count: int = 0,
) -> dict[str, Any]:
    """Execute the mark-and-sweep GC evaluation (report-only)."""
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    else:
        as_of = parse_timestamp(as_of) or datetime.now(timezone.utc)

    total_blobs = len(blobs)
    reachable_count = 0
    reachable_bytes = 0
    unreachable_count = 0
    unreachable_bytes = 0
    grace_count = 0
    grace_bytes = 0
    candidate_count = 0
    candidate_bytes = 0

    unreachable_records: list[dict[str, Any]] = []
    tombstones: list[dict[str, Any]] = []

    for digest, blob in sorted(blobs.items(), key=lambda x: x[0]):
        if digest in reachable_digests:
            reachable_count += 1
            reachable_bytes += blob.size
            continue

        unreachable_count += 1
        unreachable_bytes += blob.size

        # Compute age
        if blob.created_at:
            age_seconds = max(0.0, (as_of - blob.created_at).total_seconds())
        elif blob.mtime is not None:
            age_seconds = max(0.0, as_of.timestamp() - blob.mtime)
        else:
            # Age unknown: treat as older than grace period
            age_seconds = float("inf")

        # Resolve last referencing ActionResult
        refs = attribution.get(digest, [])
        if refs:
            # Sort by mtime/created_at descending if available
            sorted_refs = sorted(
                refs,
                key=lambda r: (r.get("mtime") or 0.0),
                reverse=True,
            )
            latest_ref = sorted_refs[0]
            last_action_key = latest_ref.get("action_key")
            last_artifact_name = latest_ref.get("artifact_name")
            last_ref_time = latest_ref.get("created_at")
        else:
            last_action_key = None
            last_artifact_name = None
            last_ref_time = None

        if age_seconds < grace_period_seconds:
            status = "grace"
            grace_count += 1
            grace_bytes += blob.size
        else:
            status = "candidate"
            candidate_count += 1
            candidate_bytes += blob.size

            # Generate Tombstone for sweep candidate
            tombstone_record = {
                "schema": 1,
                "digest": digest,
                "size": blob.size,
                "path": blob.path,
                "created_at": blob.created_at.isoformat() if blob.created_at else None,
                "unreachable_since": (
                    (as_of - datetime.resolution * 0).isoformat()
                ),
                "tombstone_time": as_of.isoformat(),
                "grace_period_seconds": grace_period_seconds,
                "last_referencing_action": last_action_key,
                "last_referencing_artifact": last_artifact_name,
                "last_referencing_time": last_ref_time,
                "reason": "unreachable_past_grace_period",
            }
            tombstones.append(tombstone_record)

        unreachable_records.append({
            "digest": digest,
            "size": blob.size,
            "size_human": format_bytes(blob.size),
            "path": blob.path,
            "created_at": blob.created_at.isoformat() if blob.created_at else None,
            "age_seconds": int(age_seconds) if not math.isinf(age_seconds) else None,
            "age_human": format_duration(age_seconds) if not math.isinf(age_seconds) else "unknown",
            "status": status,
            "last_referencing_action": last_action_key,
            "last_referencing_artifact": last_artifact_name,
            "last_referencing_time": last_ref_time,
        })

    summary = {
        "total_blobs": total_blobs,
        "total_bytes": reachable_bytes + unreachable_bytes,
        "total_bytes_human": format_bytes(reachable_bytes + unreachable_bytes),
        "reachable_blobs": reachable_count,
        "reachable_bytes": reachable_bytes,
        "reachable_bytes_human": format_bytes(reachable_bytes),
        "unreachable_blobs": unreachable_count,
        "unreachable_bytes": unreachable_bytes,
        "unreachable_bytes_human": format_bytes(unreachable_bytes),
        "grace_blobs": grace_count,
        "grace_bytes": grace_bytes,
        "grace_bytes_human": format_bytes(grace_bytes),
        "candidate_blobs": candidate_count,
        "candidate_bytes": candidate_bytes,
        "candidate_bytes_human": format_bytes(candidate_bytes),
    }

    return {
        "schema": SCHEMA,
        "mode": "report-only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "grace_period_seconds": grace_period_seconds,
        "grace_period_human": format_duration(grace_period_seconds),
        "summary": summary,
        "roots": {
            "retained_action_results_count": retained_actions_count,
            "repository_references_count": repo_sources_count,
        },
        "unreachable_blobs": unreachable_records,
        "tombstones": tombstones,
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    """Render a human-readable Markdown report for GitHub Summary or comments."""
    summary = report["summary"]
    roots = report.get("roots", {})
    as_of = report.get("as_of", "")
    grace_str = report.get("grace_period_human", "")

    lines = [
        "# CAS Garbage Collection & Reachability Report",
        "",
        "> **Mode: Report-Only** — no content blobs were modified or deleted.",
        "",
        "## Configuration & Roots",
        "",
        f"- **Evaluation Timestamp (`as_of`):** `{as_of}`",
        f"- **Grace Period:** `{grace_str}` ({report.get('grace_period_seconds', 0)} seconds)",
        f"- **Retained ActionResults:** `{roots.get('retained_action_results_count', 0)}`",
        f"- **Repository Metadata Sources:** `{roots.get('repository_references_count', 0)}`",
        "",
        "## Reachability Summary",
        "",
        "| Category | Blob Count | Total Size |",
        "|:---|---:|---:|",
        f"| **Total CAS Blobs** | {summary['total_blobs']} | {summary['total_bytes_human']} |",
        f"| **Reachable Blobs** | {summary['reachable_blobs']} | {summary['reachable_bytes_human']} |",
        f"| **Unreachable Blobs (Total)** | {summary['unreachable_blobs']} | {summary['unreachable_bytes_human']} |",
        f"| ↳ In Grace Period (< {grace_str}) | {summary['grace_blobs']} | {summary['grace_bytes_human']} |",
        f"| ↳ **Sweep Candidates (≥ {grace_str})** | **{summary['candidate_blobs']}** | **{summary['candidate_bytes_human']}** |",
        "",
    ]

    unreachable = report.get("unreachable_blobs", [])
    if unreachable:
        lines.extend([
            "## Unreachable Blobs Detail",
            "",
            "| Digest | Size | Age | Status | Last Referencing ActionResult |",
            "|:---|---:|:---|:---|:---|",
        ])
        for u in unreachable:
            digest_short = f"`{u['digest'][:19]}...`"
            size_human = u["size_human"]
            age_human = u["age_human"]
            status_badge = f"`{u['status']}`"
            if u["last_referencing_action"]:
                last_act = f"`{u['last_referencing_action'][:19]}...`"
                if u["last_referencing_artifact"]:
                    last_act += f" ({u['last_referencing_artifact']})"
            else:
                last_act = "*orphan (unreferenced)*"
            lines.append(f"| {digest_short} | {size_human} | {age_human} | {status_badge} | {last_act} |")
        lines.append("")
    else:
        lines.extend([
            "## Unreachable Blobs Detail",
            "",
            "No unreachable blobs found. All CAS blobs are reachable from retained roots.",
            "",
        ])

    tombstones = report.get("tombstones", [])
    if tombstones:
        lines.extend([
            "## Tombstones (Sweep Candidates)",
            "",
            f"A total of **{len(tombstones)}** blob(s) are eligible for tombstoning past the grace period.",
            "",
            "<details>",
            "<summary>View Tombstones Manifest</summary>",
            "",
            "```json",
            json.dumps(tombstones, indent=2, sort_keys=True),
            "```",
            "",
            "</details>",
            "",
        ])

    return "\n".join(lines)


def write_tombstones_dir(tombstones: list[dict[str, Any]], target_dir: pathlib.Path) -> None:
    """Write individual tombstone manifests to <target_dir>/tombstones/sha256/<hash>.json."""
    target_dir = pathlib.Path(target_dir)
    out_dir = target_dir / "tombstones" / "sha256"
    out_dir.mkdir(parents=True, exist_ok=True)
    for tb in tombstones:
        digest = tb["digest"]
        raw_hash = digest.removeprefix("sha256:")
        out_file = out_dir / f"{raw_hash}.json"
        out_file.write_text(json.dumps({"schema": 1, "tombstone": tb}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CAS reachability and garbage collection mark-and-sweep (report-only)"
    )
    parser.add_argument("--cas-dir", type=pathlib.Path, help="Path to CAS directory containing actions/ and blobs/")
    parser.add_argument("--actions-dir", type=pathlib.Path, help="Directory containing ActionResult manifests")
    parser.add_argument("--blobs-dir", type=pathlib.Path, help="Directory containing CAS blobs")
    parser.add_argument("--inventory-json", type=pathlib.Path, help="Path to JSON object inventory file")
    parser.add_argument("--inventory-lsf", type=pathlib.Path, help="Path to rclone lsf listing file")
    parser.add_argument("--retained-keys", type=pathlib.Path, help="Path to text file of retained action keys")
    parser.add_argument("--all-actions-dir", type=pathlib.Path, help="Directory containing all historical ActionResults for attribution")
    parser.add_argument("--factory", type=pathlib.Path, default=ROOT / "manifests" / "package-factory.yaml", help="Factory manifest for published index discovery")
    parser.add_argument("--published-index-url", action="append", default=[], help="Additional published repository URLs to scan")
    parser.add_argument("--grace-period-days", type=float, default=7.0, help="Age grace period in days (default: 7.0)")
    parser.add_argument("--grace-period-seconds", type=int, help="Age grace period in seconds (overrides days)")
    parser.add_argument("--as-of", help="Evaluation reference timestamp (ISO 8601 or UNIX timestamp)")
    parser.add_argument("--json", type=pathlib.Path, help="Write full JSON report to file")
    parser.add_argument("--markdown", type=pathlib.Path, help="Write Markdown report to file")
    parser.add_argument("--tombstones-dir", type=pathlib.Path, help="Write individual tombstone files to directory")
    parser.add_argument("--summary", action="store_true", help="Print summary table to stdout")
    args = parser.parse_args(argv)

    grace_seconds = args.grace_period_seconds if args.grace_period_seconds is not None else int(args.grace_period_days * 86400)
    as_of_dt = parse_timestamp(args.as_of) if args.as_of else datetime.now(timezone.utc)

    blobs: dict[str, BlobRecord] = {}
    action_results: dict[str, ActionResultRecord] = {}
    all_known_actions: dict[str, ActionResultRecord] = {}

    # 1. Load from CAS dir
    if args.cas_dir and args.cas_dir.is_dir():
        b, a = scan_local_cas(args.cas_dir)
        blobs.update(b)
        action_results.update(a)

    # 2. Load explicit actions/blobs dirs
    if args.actions_dir and args.actions_dir.is_dir():
        for p in args.actions_dir.rglob("*.json"):
            if p.is_file():
                try:
                    ar = load_action_result(p)
                    action_results[ar.action_key] = ar
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(f"warning: {p}: {exc}\n")

    if args.blobs_dir and args.blobs_dir.is_dir():
        for p in args.blobs_dir.rglob("*"):
            if p.is_file() and p.suffix != ".tmp":
                try:
                    d = require_sha256(p.name)
                    st = p.stat()
                    blobs[d] = BlobRecord(
                        digest=d,
                        size=st.st_size,
                        path=p.as_posix(),
                        created_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                        mtime=st.st_mtime,
                    )
                except ValueError:
                    continue

    # 3. Load historical actions dir
    if args.all_actions_dir and args.all_actions_dir.is_dir():
        for p in args.all_actions_dir.rglob("*.json"):
            if p.is_file():
                try:
                    ar = load_action_result(p)
                    all_known_actions[ar.action_key] = ar
                except Exception:  # noqa: BLE001
                    pass

    # 4. Load inventory JSON
    if args.inventory_json and args.inventory_json.is_file():
        try:
            inv_data = json.loads(args.inventory_json.read_text(encoding="utf-8"))
            if isinstance(inv_data, dict) and "objects" in inv_data:
                inv_data = inv_data["objects"]
            if isinstance(inv_data, list):
                b, a = parse_inventory_json(inv_data)
                blobs.update(b)
                action_results.update(a)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"warning: error loading inventory JSON {args.inventory_json}: {exc}\n")

    # 5. Load inventory lsf
    if args.inventory_lsf and args.inventory_lsf.is_file():
        try:
            lsf_lines = args.inventory_lsf.read_text(encoding="utf-8").splitlines()
            b = parse_inventory_lsf(lsf_lines)
            blobs.update(b)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"warning: error loading inventory LSF {args.inventory_lsf}: {exc}\n")

    # 6. Filter retained ActionResults if list of keys provided
    retained_actions = action_results
    if args.retained_keys and args.retained_keys.is_file():
        keys_set = {
            require_sha256(line.strip())
            for line in args.retained_keys.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        # Keep non-retained in all_known_actions
        for k, v in action_results.items():
            if k not in keys_set:
                all_known_actions[k] = v
        retained_actions = {k: v for k, v in action_results.items() if k in keys_set}

    # 7. Scan repository metadata
    repo_refs = scan_repo_metadata(
        factory_manifest=args.factory if args.factory and args.factory.is_file() else None,
        repo_urls=args.published_index_url,
    )

    # 8. Compute Reachability & Mark phase
    reachable_digests, attribution = compute_reachability(
        retained_actions=retained_actions.values(),
        repo_references=repo_refs,
        all_known_actions=all_known_actions.values(),
    )

    # 9. Sweep phase (Report-Only)
    report = run_mark_and_sweep(
        blobs=blobs,
        reachable_digests=reachable_digests,
        attribution=attribution,
        grace_period_seconds=grace_seconds,
        as_of=as_of_dt,
        retained_actions_count=len(retained_actions),
        repo_sources_count=len(repo_refs),
    )

    md_report = render_markdown_report(report)

    # 10. Outputs
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote JSON report: {args.json}")

    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(md_report + "\n", encoding="utf-8")
        print(f"wrote Markdown report: {args.markdown}")

    if args.tombstones_dir:
        write_tombstones_dir(report["tombstones"], args.tombstones_dir)
        print(f"wrote {len(report['tombstones'])} tombstone(s) to: {args.tombstones_dir}")

    if args.summary or (not args.json and not args.markdown and not args.tombstones_dir):
        print(md_report)

    # Set GITHUB_STEP_SUMMARY if available
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary and os.path.exists(gh_summary):
        try:
            with open(gh_summary, "a", encoding="utf-8") as f:
                f.write("\n" + md_report + "\n")
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
