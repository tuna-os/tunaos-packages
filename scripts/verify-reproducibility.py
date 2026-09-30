#!/usr/bin/env python3
"""Sampled reproducibility rebuilds and verification for Tideforge ActionResults.

RFC 011 / PR #430 operational transition, Step 6 (tunaos-packages#486).
Rebuilds sampled coordinates from their recorded semantic inputs, compares
artifact digests against stored ActionResults, flags divergent recipes as
quarantined/non-cacheable, and tracks reproducibility policy metrics over time.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import sys
from typing import Any, Iterable

import yaml

import importlib.util

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import factory_contract  # noqa: E402

_planner_spec = importlib.util.spec_from_file_location(
    "factory_planner", ROOT / "scripts" / "plan-package-factory.py"
)
factory_planner = importlib.util.module_from_spec(_planner_spec)
_planner_spec.loader.exec_module(factory_planner)

_cache_spec = importlib.util.spec_from_file_location(
    "action_cache", ROOT / "scripts" / "tideforge-action-cache.py"
)
action_cache = importlib.util.module_from_spec(_cache_spec)
_cache_spec.loader.exec_module(action_cache)

SCHEMA = 1
HISTORY_LIMIT = 120
DEFAULT_QUARANTINE_PATH = ROOT / "manifests" / "reproducibility-quarantine.yaml"
DEFAULT_STATUS_PATH = ROOT / "docs" / "reproducibility-status.json"


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def save_yaml(data: Any, path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def load_json(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def save_json(data: Any, path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_quarantine(path: pathlib.Path = DEFAULT_QUARANTINE_PATH) -> dict[str, Any]:
    data = load_yaml(path)
    if not data or data.get("schema") != SCHEMA:
        return {"schema": SCHEMA, "quarantined_packages": []}
    if not isinstance(data.get("quarantined_packages"), list):
        data["quarantined_packages"] = []
    return data


def is_quarantined(
    quarantine: dict[str, Any],
    package: str,
    target: str | None = None,
    arch: str | None = None,
) -> bool:
    for entry in quarantine.get("quarantined_packages", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("package") != package:
            continue
        if target and entry.get("target") and entry.get("target") != target:
            continue
        if arch and entry.get("architecture") and entry.get("architecture") != arch:
            continue
        return True
    return False


def add_to_quarantine(
    quarantine: dict[str, Any],
    package: str,
    target: str,
    arch: str,
    engine: str,
    format_name: str,
    reason: str,
    divergent_artifacts: list[dict[str, Any]] | None = None,
    quarantined_at: str | None = None,
) -> bool:
    if "quarantined_packages" not in quarantine or not isinstance(quarantine["quarantined_packages"], list):
        quarantine["quarantined_packages"] = []

    now = quarantined_at or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    existing = None
    for entry in quarantine["quarantined_packages"]:
        if (
            isinstance(entry, dict)
            and entry.get("package") == package
            and entry.get("target") == target
            and entry.get("architecture") == arch
        ):
            existing = entry
            break

    record = {
        "package": package,
        "target": target,
        "architecture": arch,
        "engine": engine,
        "format": format_name,
        "quarantined_at": now,
        "reason": reason,
        "divergent_artifacts": divergent_artifacts or [],
    }

    if existing is not None:
        existing.update(record)
        return False
    quarantine["quarantined_packages"].append(record)
    return True


def remove_from_quarantine(
    quarantine: dict[str, Any],
    package: str,
    target: str | None = None,
    arch: str | None = None,
) -> bool:
    entries = quarantine.get("quarantined_packages", [])
    initial_len = len(entries)
    filtered = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("package") == package:
            if target and entry.get("target") != target:
                filtered.append(entry)
                continue
            if arch and entry.get("architecture") != arch:
                filtered.append(entry)
                continue
            # Match: skip to remove
            continue
        filtered.append(entry)
    quarantine["quarantined_packages"] = filtered
    return len(filtered) < initial_len


def sample_cells(
    root: pathlib.Path,
    sample_size: int = 1,
    engine: str | None = None,
    format_name: str | None = None,
    target: str | None = None,
    arch: str | None = None,
    canary_only: bool = False,
) -> list[dict[str, Any]]:
    """Sample candidate cells across engines, formats, and architectures."""
    cells = factory_planner.all_cells(root)
    filtered = []
    for cell in cells:
        if engine and cell.get("engine") != engine:
            continue
        if format_name and cell.get("format") != format_name:
            continue
        if target and cell.get("target") != target:
            continue
        if arch and cell.get("architecture") != arch:
            continue
        if canary_only and not cell.get("canary"):
            continue
        filtered.append(cell)

    # Group by coordinate (engine, format, architecture)
    coordinates: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for cell in sorted(filtered, key=lambda c: c["id"]):
        coord = (str(cell["engine"]), str(cell["format"]), str(cell["architecture"]))
        coordinates.setdefault(coord, []).append(cell)

    sampled: list[dict[str, Any]] = []
    for _coord, bucket in sorted(coordinates.items()):
        # Prioritize canary cells within each coordinate if present
        canaries = [c for c in bucket if c.get("canary")]
        non_canaries = [c for c in bucket if not c.get("canary")]
        ordered = canaries + non_canaries
        count = min(sample_size, len(ordered))
        sampled.extend(ordered[:count])

    return sorted(sampled, key=lambda c: c["id"])


def digest_artifacts_dir(artifact_dir: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Scan and digest all regular files in an artifact directory."""
    artifacts: dict[str, dict[str, Any]] = {}
    if not artifact_dir.is_dir():
        return artifacts
    for path in sorted(artifact_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        name = action_cache.safe_artifact_name(path.name)
        if name in artifacts:
            raise ValueError(f"duplicate artifact name: {name}")
        artifacts[name] = {
            "name": name,
            "size": path.stat().st_size,
            "digest": action_cache.digest_file(path),
        }
    return artifacts


def compare_artifact_maps(
    expected_artifacts: dict[str, dict[str, Any]],
    actual_artifacts: dict[str, dict[str, Any]],
    action_key: str = "",
) -> dict[str, Any]:
    """Compare two sets of artifact records."""
    expected_names = set(expected_artifacts.keys())
    actual_names = set(actual_artifacts.keys())

    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names)
    common = sorted(expected_names & actual_names)

    matching = []
    mismatches = []

    for name in common:
        exp = expected_artifacts[name]
        act = actual_artifacts[name]
        if exp["digest"] == act["digest"] and exp["size"] == act["size"]:
            matching.append(name)
        else:
            mismatches.append(
                {
                    "name": name,
                    "expected_digest": exp["digest"],
                    "actual_digest": act["digest"],
                    "expected_size": exp["size"],
                    "actual_size": act["size"],
                }
            )

    reproducible = len(missing) == 0 and len(extra) == 0 and len(mismatches) == 0 and len(matching) > 0

    return {
        "action_key": action_key,
        "reproducible": reproducible,
        "matching": matching,
        "missing": missing,
        "extra": extra,
        "mismatches": mismatches,
    }


def compare_against_result(
    expected_result: dict[str, Any],
    rebuilt_dir: pathlib.Path,
    expected_action_key: str | None = None,
) -> dict[str, Any]:
    """Compare a rebuilt artifacts directory against a stored ActionResult."""
    if expected_result.get("schema") != SCHEMA:
        raise ValueError("unsupported ActionResult schema")

    key = str(expected_result.get("action_key", ""))
    if expected_action_key and key != expected_action_key:
        raise ValueError(f"action key mismatch: expected {expected_action_key}, got {key}")

    expected_artifacts = {
        item["name"]: {
            "name": item["name"],
            "size": item["size"],
            "digest": item["digest"],
        }
        for item in expected_result.get("artifacts", [])
    }

    actual_artifacts = digest_artifacts_dir(rebuilt_dir)
    verdict = compare_artifact_maps(expected_artifacts, actual_artifacts, action_key=key)
    return verdict


def compare_two_dirs(
    dir_a: pathlib.Path,
    dir_b: pathlib.Path,
    action_key: str = "",
) -> dict[str, Any]:
    """Compare artifacts from two independent build directories."""
    artifacts_a = digest_artifacts_dir(dir_a)
    artifacts_b = digest_artifacts_dir(dir_b)
    return compare_artifact_maps(artifacts_a, artifacts_b, action_key=action_key)


def compute_policy_report(
    verification_results: list[dict[str, Any]],
    quarantine: dict[str, Any] | None = None,
    previous_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate aggregate reproducibility rates across formats, engines, and coordinates."""
    quarantine_data = quarantine or {"schema": SCHEMA, "quarantined_packages": []}
    quarantined_list = quarantine_data.get("quarantined_packages", [])

    total_sampled = len(verification_results)
    total_reproducible = sum(1 for r in verification_results if r.get("reproducible"))
    total_divergent = total_sampled - total_reproducible
    overall_rate = round((total_reproducible / total_sampled * 100.0), 1) if total_sampled > 0 else 100.0

    by_engine: dict[str, dict[str, Any]] = {}
    by_format: dict[str, dict[str, Any]] = {}
    by_coordinate: dict[str, dict[str, Any]] = {}

    for res in verification_results:
        eng = res.get("engine", "unknown")
        fmt = res.get("format", "unknown")
        arch = res.get("architecture", "unknown")
        is_repro = bool(res.get("reproducible"))

        for key, mapping in (
            (eng, by_engine),
            (fmt, by_format),
            (f"{eng}/{fmt}/{arch}", by_coordinate),
        ):
            if key not in mapping:
                mapping[key] = {"sampled": 0, "reproducible": 0, "divergent": 0, "rate": 0.0}
            mapping[key]["sampled"] += 1
            if is_repro:
                mapping[key]["reproducible"] += 1
            else:
                mapping[key]["divergent"] += 1
            mapping[key]["rate"] = round(
                (mapping[key]["reproducible"] / mapping[key]["sampled"] * 100.0), 1
            )

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    history = list((previous_report or {}).get("history") or [])
    if previous_report and previous_report.get("measured_at"):
        entry = {
            "measured_at": previous_report["measured_at"],
            "summary": previous_report.get("summary", {}),
        }
        if not history or history[-1]["measured_at"] != entry["measured_at"]:
            history.append(entry)

    report = {
        "schema": SCHEMA,
        "measured_at": now,
        "summary": {
            "sampled": total_sampled,
            "reproducible": total_reproducible,
            "divergent": total_divergent,
            "quarantined": len(quarantined_list),
            "reproducibility_rate": overall_rate,
        },
        "by_engine": by_engine,
        "by_format": by_format,
        "by_coordinate": by_coordinate,
        "results": verification_results,
        "quarantined": quarantined_list,
        "history": history[-HISTORY_LIMIT:],
    }
    return report


def render_policy_markdown(report: dict[str, Any]) -> str:
    """Render the reproducibility policy section for FACTORY-STATUS.md."""
    lines = [
        "## Reproducibility verification",
        "",
        "Sampled rebuilds against recorded ActionResults to monitor determinism",
        "and cache-reuse guarantees (#486):",
        "",
    ]

    summary = report.get("summary", {})
    rate = summary.get("reproducibility_rate", 100.0)
    sampled = summary.get("sampled", 0)
    repro = summary.get("reproducible", 0)
    div = summary.get("divergent", 0)
    quar = summary.get("quarantined", 0)

    lines.append(
        f"Overall: **{rate}%** reproducible ({repro}/{sampled} sampled rebuilds verified; "
        f"{quar} quarantined)."
    )
    lines.append("")

    lines.append("| Engine | Format | Sampled | Reproducible | Divergent | Rate |")
    lines.append("|---|---|---|---|---|---|")

    by_coord = report.get("by_coordinate", {})
    if by_coord:
        for coord in sorted(by_coord.keys()):
            data = by_coord[coord]
            parts = coord.split("/")
            eng = parts[0] if len(parts) > 0 else "—"
            fmt = parts[1] if len(parts) > 1 else "—"
            arch = parts[2] if len(parts) > 2 else ""
            label = f"{eng} ({arch})" if arch else eng
            lines.append(
                f"| {label} | {fmt} | {data['sampled']} | {data['reproducible']} | "
                f"{data['divergent']} | {data['rate']}% |"
            )
    else:
        # Fallback table if no detailed coordinate breakdown
        by_engine = report.get("by_engine", {})
        for eng, data in sorted(by_engine.items()):
            lines.append(
                f"| {eng} | all | {data['sampled']} | {data['reproducible']} | "
                f"{data['divergent']} | {data['rate']}% |"
            )
    lines.append("")

    quarantined = report.get("quarantined", [])
    if quarantined:
        lines.append("**Quarantined recipes (divergent artifact digests detected upon rebuild):**")
        lines.append("")
        for q in quarantined:
            pkg = q.get("package", "unknown")
            tgt = q.get("target", "unknown")
            arch = q.get("architecture", "unknown")
            reason = q.get("reason", "divergent digests")
            lines.append(f"- `{pkg}` ({tgt}/{arch}) — {reason}")
        lines.append("")
    else:
        lines.append("No recipes currently quarantined.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    # Subcommand: sample
    sample_parser = commands.add_parser("sample", help="Sample cells for rebuild verification")
    sample_parser.add_argument("--root", type=pathlib.Path, default=ROOT)
    sample_parser.add_argument("--sample-size", type=int, default=1)
    sample_parser.add_argument("--engine")
    sample_parser.add_argument("--format", dest="format_name")
    sample_parser.add_argument("--target")
    sample_parser.add_argument("--arch")
    sample_parser.add_argument("--canary-only", action="store_true")
    sample_parser.add_argument("--github-output", type=pathlib.Path)
    sample_parser.add_argument("--json", action="store_true")

    # Subcommand: verify (rebuilt dir vs ActionResult)
    verify_parser = commands.add_parser("verify", help="Verify rebuilt directory against ActionResult")
    verify_parser.add_argument("--expected-result", type=pathlib.Path, required=True)
    verify_parser.add_argument("--rebuilt-dir", type=pathlib.Path, required=True)
    verify_parser.add_argument("--action-key")
    verify_parser.add_argument("--cell-id", default="")
    verify_parser.add_argument("--package", default="")
    verify_parser.add_argument("--target", default="")
    verify_parser.add_argument("--arch", default="")
    verify_parser.add_argument("--engine", default="")
    verify_parser.add_argument("--format", dest="format_name", default="")
    verify_parser.add_argument("--quarantine-file", type=pathlib.Path, default=DEFAULT_QUARANTINE_PATH)
    verify_parser.add_argument("--record-quarantine", action="store_true")
    verify_parser.add_argument("--output-json", type=pathlib.Path)

    # Subcommand: compare-dirs (build A vs build B)
    cmp_parser = commands.add_parser("compare-dirs", help="Compare two independent build artifact dirs")
    cmp_parser.add_argument("--dir-a", type=pathlib.Path, required=True)
    cmp_parser.add_argument("--dir-b", type=pathlib.Path, required=True)
    cmp_parser.add_argument("--action-key", default="")
    cmp_parser.add_argument("--cell-id", default="")
    cmp_parser.add_argument("--package", default="")
    cmp_parser.add_argument("--target", default="")
    cmp_parser.add_argument("--arch", default="")
    cmp_parser.add_argument("--engine", default="")
    cmp_parser.add_argument("--format", dest="format_name", default="")
    cmp_parser.add_argument("--quarantine-file", type=pathlib.Path, default=DEFAULT_QUARANTINE_PATH)
    cmp_parser.add_argument("--record-quarantine", action="store_true")
    cmp_parser.add_argument("--output-json", type=pathlib.Path)

    # Subcommand: collect
    collect_parser = commands.add_parser("collect", help="Collect verification JSONs and generate report")
    collect_parser.add_argument("--results-dir", type=pathlib.Path, required=True)
    collect_parser.add_argument("--quarantine-file", type=pathlib.Path, default=DEFAULT_QUARANTINE_PATH)
    collect_parser.add_argument("--out-json", type=pathlib.Path, default=DEFAULT_STATUS_PATH)
    collect_parser.add_argument("--out-md", type=pathlib.Path)

    # Subcommand: quarantine
    quar_parser = commands.add_parser("quarantine", help="Inspect or modify quarantine manifest")
    quar_parser.add_argument("--quarantine-file", type=pathlib.Path, default=DEFAULT_QUARANTINE_PATH)
    quar_parser.add_argument("--check")
    quar_parser.add_argument("--target")
    quar_parser.add_argument("--arch")
    quar_parser.add_argument("--list", action="store_true")

    args = parser.parse_args()

    if args.command == "sample":
        sampled = sample_cells(
            args.root,
            sample_size=args.sample_size,
            engine=args.engine,
            format_name=args.format_name,
            target=args.target,
            arch=args.arch,
            canary_only=args.canary_only,
        )
        if args.json:
            print(json.dumps(sampled, indent=2, sort_keys=True))
        else:
            matrix_json = json.dumps({"include": sampled}, separators=(",", ":"))
            print(f"Sampled {len(sampled)} cells across coordinates")
            if args.github_output:
                with args.github_output.open("a", encoding="utf-8") as out:
                    out.write(f"count={len(sampled)}\n")
                    out.write(f"matrix={matrix_json}\n")

    elif args.command == "verify":
        expected = json.loads(args.expected_result.read_text(encoding="utf-8"))
        verdict = compare_against_result(expected, args.rebuilt_dir, args.action_key)
        verdict.update({
            "cell_id": args.cell_id,
            "package": args.package,
            "target": args.target,
            "architecture": args.arch,
            "engine": args.engine,
            "format": args.format_name,
        })
        if not verdict["reproducible"] and args.record_quarantine and args.package:
            quar = load_quarantine(args.quarantine_file)
            reason = f"Divergent artifact digests upon rebuild: {verdict['mismatches']}"
            add_to_quarantine(
                quar,
                args.package,
                args.target,
                args.arch,
                args.engine,
                args.format_name,
                reason,
                verdict["mismatches"],
            )
            save_yaml(quar, args.quarantine_file)
        if args.output_json:
            save_json(verdict, args.output_json)
        print(json.dumps(verdict, indent=2, sort_keys=True))
        return 0 if verdict["reproducible"] else 1

    elif args.command == "compare-dirs":
        verdict = compare_two_dirs(args.dir_a, args.dir_b, args.action_key)
        verdict.update({
            "cell_id": args.cell_id,
            "package": args.package,
            "target": args.target,
            "architecture": args.arch,
            "engine": args.engine,
            "format": args.format_name,
        })
        if not verdict["reproducible"] and args.record_quarantine and args.package:
            quar = load_quarantine(args.quarantine_file)
            reason = f"Artifact digest divergence between independent builds: {verdict['mismatches']}"
            add_to_quarantine(
                quar,
                args.package,
                args.target,
                args.arch,
                args.engine,
                args.format_name,
                reason,
                verdict["mismatches"],
            )
            save_yaml(quar, args.quarantine_file)
        if args.output_json:
            save_json(verdict, args.output_json)
        print(json.dumps(verdict, indent=2, sort_keys=True))
        return 0 if verdict["reproducible"] else 1

    elif args.command == "collect":
        results = []
        if args.results_dir.is_dir():
            for f in sorted(args.results_dir.rglob("*.json")):
                if f.name == "reproducibility-status.json" or f.name.startswith("package-"):
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and "reproducible" in data:
                        results.append(data)
                except (json.JSONDecodeError, OSError):
                    continue

        previous = load_json(args.out_json)
        quar = load_quarantine(args.quarantine_file)
        report = compute_policy_report(results, quarantine=quar, previous_report=previous)
        save_json(report, args.out_json)
        if args.out_md:
            args.out_md.write_text(render_policy_markdown(report), encoding="utf-8")
        print(f"Collected {len(results)} verification results; report written to {args.out_json}")

    elif args.command == "quarantine":
        quar = load_quarantine(args.quarantine_file)
        if args.check:
            quarantined = is_quarantined(quar, args.check, args.target, args.arch)
            print("quarantined" if quarantined else "not quarantined")
            return 0 if quarantined else 1
        if args.list:
            print(json.dumps(quar, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
