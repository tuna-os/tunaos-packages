#!/usr/bin/env python3
"""Audit upstream Bluefin/Aurora/Zirconium package declarations against the
TunaOS recipe set.

docs/UPSTREAM_PARITY.md registers Bluefin, Aurora and Zirconium as the
upstreams whose curated desktop experiences TunaOS carries forward.  This
script turns that register into a checkable invariant: every package an
upstream snapshot (_upstream-snapshots/<flavor>.yaml) declares must resolve
against this repository to exactly one of

    recipe        a TunaOS source recipe (packages/<name>/package.yaml) or a
                  reviewed spec tree (src/<tree>/<name>) exists
    manifest      declared in a TunaOS desktop manifest install/required list
    distro        consumed from the base distribution, no rebuild; the entry
                  must name the owning distribution
    out-of-scope  explicitly tracked as not carried; the entry must carry a
                  reason

A package with no disposition, an unknown disposition, or a disposition the
repository does not honour is an uncovered gap and fails --strict.  The
snapshot records the upstream revision each declaration was taken from, so a
re-run after an upstream bump shows exactly which packages moved.

Usage:
    scripts/audit-upstream-parity.py
    scripts/audit-upstream-parity.py --strict
    scripts/audit-upstream-parity.py --report-json /tmp/upstream-parity.json
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

import yaml

DISPOSITIONS = ("recipe", "manifest", "distro", "out-of-scope")


def recipe_index(root: pathlib.Path) -> dict[str, str]:
    """name -> provenance for every package this repository can build.

    recipes: packages/<name>/package.yaml (Tideforge source recipes).
    spec trees: src/<tree>/<name> directories (reviewed RPM specs).
    """
    index: dict[str, str] = {}
    recipes = root / "packages"
    for recipe_dir in sorted(recipes.iterdir()) if recipes.is_dir() else []:
        package_yaml = recipe_dir / "package.yaml"
        if not package_yaml.is_file():
            continue
        data = yaml.safe_load(package_yaml.read_text())
        name = data.get("name") if isinstance(data, dict) else None
        if name:
            index[name] = f"packages/{recipe_dir.name}"
    src = root / "src"
    for tree in sorted(src.iterdir()) if src.is_dir() else []:
        if not tree.is_dir():
            continue
        for spec_dir in sorted(tree.iterdir()):
            if spec_dir.is_dir():
                index[spec_dir.name] = f"src/{tree.name}/{spec_dir.name}"
    return index


def manifest_names(root: pathlib.Path) -> set[str]:
    """Package names TunaOS desktop manifests declare as installed/required."""
    names: set[str] = set()

    def collect(*values) -> None:
        for value in values:
            if isinstance(value, list):
                names.update(v for v in value if isinstance(v, str))
            elif isinstance(value, dict):
                # Only list-valued keys hold package names; string values such
                # as `source:` provenance are not package names.
                for key in ("install_packages", "required_packages", "roots", "needs"):
                    collect(value.get(key))

    hummingbird = root / "manifests" / "hummingbird-desktops.yaml"
    if hummingbird.is_file():
        data = yaml.safe_load(hummingbird.read_text()) or {}
        for desktop in data.get("desktops", {}).values():
            collect(desktop.get("install_packages"), desktop.get("required_packages"))
        collect(*data.get("components", {}).values())

    for manifest_dir in ("dependency-trees", "target-queues"):
        directory = root / "manifests" / manifest_dir
        if not directory.is_dir():
            continue
        for manifest in sorted(directory.glob("*.yaml")):
            data = yaml.safe_load(manifest.read_text()) or {}
            if manifest_dir == "dependency-trees":
                collect(*data.get("nodes", {}).keys())
                for node in data.get("nodes", {}).values():
                    collect(node.get("needs"))
            else:
                for queue in data.get("queues", {}).values():
                    collect(queue.get("roots"))
    return names


def audit_package(entry: dict, recipes: dict[str, str], manifests: set[str]) -> dict:
    """Resolve one snapshot declaration, or record why it cannot resolve."""
    name = entry.get("name")
    disposition = entry.get("disposition")
    result = {
        "name": name,
        "source": entry.get("source"),
        "disposition": disposition,
        "verified": False,
        "provenance": None,
        "note": entry.get("note"),
        "issues": [],
    }
    if not name:
        result["issues"].append("entry has no name")
        return result
    if disposition not in DISPOSITIONS:
        result["issues"].append(
            f"missing or unknown disposition {disposition!r}; expected one of "
            + ", ".join(DISPOSITIONS)
        )
        return result
    if disposition == "recipe":
        provenance = recipes.get(name)
        if provenance:
            result["verified"] = True
            result["provenance"] = provenance
        else:
            result["issues"].append(
                f"declared recipe but no packages/{name}/package.yaml or "
                "src/*/<name> spec tree exists"
            )
    elif disposition == "manifest":
        if name in manifests:
            result["verified"] = True
            result["provenance"] = "manifest"
        else:
            result["issues"].append(
                f"declared manifest but {name} is in no desktop "
                "install/required list"
            )
    elif disposition == "distro":
        # Consumption from the base distribution cannot be verified against
        # this repository; the snapshot must at least name the distribution
        # it trusts, so the declaration is traceable.
        if entry.get("distro"):
            result["verified"] = True
            result["provenance"] = f"distro:{entry['distro']}"
        else:
            result["issues"].append("distro disposition must name the owning distribution")
    elif disposition == "out-of-scope":
        if entry.get("reason"):
            result["verified"] = True
            result["provenance"] = "out-of-scope"
        else:
            result["issues"].append("out-of-scope disposition must carry a reason")
    return result


def audit_snapshot(snapshot: dict, recipes: dict[str, str], manifests: set[str]) -> dict:
    packages = [
        audit_package(entry, recipes, manifests) for entry in snapshot.get("packages", [])
    ]
    return {
        "upstream": snapshot.get("upstream"),
        "flavor": snapshot.get("flavor"),
        "revision": snapshot.get("revision"),
        "snapshotted_at": str(snapshot.get("snapshotted_at"))
        if snapshot.get("snapshotted_at")
        else None,
        "scope": snapshot.get("scope"),
        "packages": packages,
        "covered": sum(1 for p in packages if p["verified"]),
        "gaps": [p for p in packages if p["issues"]],
    }


def audit_all(snapshots_dir: pathlib.Path, root: pathlib.Path) -> dict:
    recipes = recipe_index(root)
    manifests = manifest_names(root)
    snapshots = {}
    for path in sorted(snapshots_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        snapshots[path.stem] = audit_snapshot(data, recipes, manifests)
    total = sum(len(s["packages"]) for s in snapshots.values())
    covered = sum(s["covered"] for s in snapshots.values())
    return {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "recipe_index_entries": len(recipes),
        "snapshots": snapshots,
        "summary": {
            "total": total,
            "covered": covered,
            "gaps": total - covered,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshots",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parents[1] / "_upstream-snapshots",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--strict", action="store_true", help="exit 1 on any gap")
    parser.add_argument("--report-json", type=pathlib.Path)
    args = parser.parse_args()

    report = audit_all(args.snapshots, args.root)
    for flavor, snapshot in report["snapshots"].items():
        print(
            f"{flavor:14} {snapshot['covered']}/{len(snapshot['packages'])} "
            f"declarations covered ({snapshot['revision'][:12]})",
            file=sys.stderr,
        )
        for gap in snapshot["gaps"]:
            print(f"  GAP {gap['name']}: {'; '.join(gap['issues'])}", file=sys.stderr)
    print(
        f"total: {report['summary']['covered']}/{report['summary']['total']} "
        f"declarations covered, {report['summary']['gaps']} gap(s)",
        file=sys.stderr,
    )

    if args.report_json:
        args.report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"wrote {args.report_json}", file=sys.stderr)

    print(json.dumps(report, indent=2, sort_keys=True))
    if args.strict and report["summary"]["gaps"]:
        raise SystemExit(f"upstream parity audit failed: {report['summary']['gaps']} gap(s)")


if __name__ == "__main__":
    main()
