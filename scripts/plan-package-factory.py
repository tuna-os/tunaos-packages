#!/usr/bin/env python3
"""Emit the one package-factory matrix from recipes and native queue data."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from typing import Any

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import factory_contract  # noqa: E402  (needs the path above)


RECIPE_CHANGE = re.compile(r"^packages/([^/]+)/")
COMMON_INPUTS = {
    ".github/workflows/package-factory.yml",
    ".github/workflows/package-factory-cell.yml",
    ".github/actions/tideforge-action-cache/action.yml",
    "scripts/run-package-factory-cell.sh",
    "scripts/verify-package-factory-cell.sh",
    "scripts/tideforge-action-cache.py",
    "scripts/tideforge.py",
}
FORMAT_INPUTS = {
    "scripts/assemble-deb-source-tree.py": {"deb"},
    "scripts/arch-clean-install.sh": {"pkg.tar.zst"},
}
NATIVE_INPUTS = {"scripts/build-chain.sh", "scripts/parse-build-order.py"}
DISTGIT_INPUTS = {"scripts/import-fedora-distgit.py"}
DEPENDENCY_TREE_CHANGE = re.compile(r"^manifests/dependency-trees/[^/]+\.ya?ml$")
TARGET_QUEUE_CHANGE = re.compile(r"^manifests/target-queues/[^/]+\.ya?ml$")


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a mapping")
    return value


def runner_for(architecture: str) -> str:
    return "ubuntu-24.04-arm" if architecture in {"aarch64", "arm64"} else "ubuntu-24.04"


def tideforge_cells(root: pathlib.Path) -> list[dict[str, Any]]:
    factory = load_yaml(root / "manifests/package-factory.yaml")
    cells = []
    for recipe_path in sorted((root / "packages").glob("*/package.yaml")):
        if recipe_path.parent.name.startswith("_"):
            continue
        recipe = load_yaml(recipe_path)
        package = str(recipe.get("name") or recipe_path.parent.name)
        dependencies = recipe.get("dependencies") or {}
        capabilities = sorted(
            {
                str(capability)
                for phase in ("build", "runtime")
                for capability in ((dependencies.get(phase) or {}).get("capabilities") or [])
            }
        )
        for target_id in recipe.get("targets") or []:
            target = (factory.get("targets") or {}).get(target_id)
            if not isinstance(target, dict):
                raise ValueError(f"{recipe_path}: unknown target {target_id}")
            image = target.get("probe_image")
            package_format = target.get("format")
            if not image or not package_format:
                raise ValueError(f"{recipe_path}: incomplete target contract {target_id}")
            architectures = target.get("architectures") or []
            for architecture in architectures:
                # Arch's official container is x86_64-only. The target contract
                # may advertise future aarch64 support, but no action is emitted
                # until it declares a target-native image for that architecture.
                if target_id == "arch" and architecture != "x86_64":
                    continue
                cells.append(
                    {
                        "id": factory_contract.tideforge_cell_id(package, target_id, architecture),
                        "engine": "tideforge",
                        "package": package,
                        "recipe": recipe_path.relative_to(root).as_posix(),
                        "target": target_id,
                        "format": package_format,
                        "architecture": architecture,
                        "image": image,
                        "verify_image": image,
                        "runner": runner_for(str(architecture)),
                        "source_paths": [recipe_path.parent.relative_to(root).as_posix() + "/"],
                        "manifest": "",
                        "mock_config": "",
                        "family": "tideforge",
                        "r2_path": str(target.get("r2_path") or ""),
                        "tiers": "",
                        "canary_tiers": "",
                        "capabilities": capabilities,
                        "track": "stable",
                        "series": str(recipe.get("version") or ""),
                        "dependency_tree": "",
                        "target_queue": "",
                        "canary": bool((recipe.get("ci") or {}).get("canary", False)),
                        "uses_distgit": False,
                    }
                )
    return cells


def native_cells(root: pathlib.Path) -> list[dict[str, Any]]:
    registry = load_yaml(root / "manifests/package-builds.yaml")
    cells = []
    for raw in registry.get("native_builds") or []:
        if not isinstance(raw, dict):
            raise ValueError("native build entries must be mappings")
        cell = dict(raw)
        if cell.get("enabled", True) is False:
            continue
        cell.pop("enabled", None)
        required = {"id", "target", "architecture", "image", "manifest", "mock_config", "source_paths"}
        missing = sorted(required - cell.keys())
        if missing:
            raise ValueError(f"native build {cell.get('id', '<unknown>')} misses {missing}")
        cell.update(
            {
                "engine": "build-chain",
                "format": "rpm",
                "runner": cell.get("runner") or runner_for(str(cell["architecture"])),
                "verify_image": str(cell.get("verify_image") or cell["image"]),
                "package": "",
                "recipe": "",
                "family": str(cell.get("family") or "native"),
                "r2_path": str(cell.get("r2_path") or ""),
                "tiers": str(cell.get("tiers") or ""),
                "canary_tiers": str(cell.get("canary_tiers") or ""),
                "capabilities": [],
                "track": str(cell.get("track") or "stable"),
                "series": str(cell.get("series") or ""),
                "dependency_tree": str(cell.get("dependency_tree") or ""),
                "target_queue": str(cell.get("target_queue") or ""),
                "canary": bool(cell.get("canary", False)),
                "uses_distgit": "distgit:" in (root / str(cell["manifest"])).read_text(encoding="utf-8"),
            }
        )
        cells.append(cell)
    return cells


def all_cells(root: pathlib.Path) -> list[dict[str, Any]]:
    cells = tideforge_cells(root) + native_cells(root)
    ids = [cell["id"] for cell in cells]
    if len(ids) != len(set(ids)):
        raise ValueError("package factory cell IDs must be unique")
    return sorted(cells, key=lambda cell: cell["id"])


def affected_formats(changed: set[str]) -> set[str] | None:
    if changed & COMMON_INPUTS:
        return None
    formats = set()
    for path, selected in FORMAT_INPUTS.items():
        if path in changed:
            formats.update(selected)
    return formats


def select_cells(
    cells: list[dict[str, Any]],
    changed_files: list[str] | None,
    *,
    changed_targets: set[str] | None = None,
    changed_native_ids: set[str] | None = None,
    changed_capabilities: set[tuple[str, str]] | None = None,
    changed_graph_ids: set[str] | None = None,
    canary_common: bool = False,
) -> list[dict[str, Any]]:
    if changed_files is None:
        return cells
    changed = {path.strip() for path in changed_files if path.strip()}
    if not changed:
        return []
    formats = affected_formats(changed)
    if formats is None:
        return canary_cells(cells) if canary_common else cells
    if "manifests/package-factory.yaml" in changed:
        if changed_targets is None:
            return cells
        return [
            cell
            for cell in cells
            if cell["target"] in changed_targets
            or (
                cell["engine"] == "tideforge"
                and changed_capabilities is not None
                and any((capability, cell["target"]) in changed_capabilities for capability in cell["capabilities"])
            )
        ]
    if "manifests/package-builds.yaml" in changed:
        if changed_native_ids is None:
            return cells
        return [cell for cell in cells if cell["id"] in changed_native_ids]
    graph_paths = {
        path for path in changed if DEPENDENCY_TREE_CHANGE.match(path) or TARGET_QUEUE_CHANGE.match(path)
    }
    if graph_paths:
        graph_cells = [
            cell
            for cell in cells
            if cell["dependency_tree"] in graph_paths or cell["target_queue"] in graph_paths
        ]
        if changed_graph_ids is None:
            return graph_cells
        return [cell for cell in graph_cells if cell["id"] in changed_graph_ids]
    changed_packages = {match.group(1) for path in changed if (match := RECIPE_CHANGE.match(path))}
    selected = []
    for cell in cells:
        if cell["engine"] == "tideforge" and cell["package"] in changed_packages:
            selected.append(cell)
            continue
        if formats and cell["format"] in formats:
            selected.append(cell)
            continue
        if changed & NATIVE_INPUTS and cell["engine"] == "build-chain":
            selected.append(cell)
            continue
        if changed & DISTGIT_INPUTS and cell["engine"] == "build-chain" and cell["uses_distgit"]:
            selected.append(cell)
            continue
        if cell["engine"] == "build-chain" and any(
            path == cell["manifest"] or any(path.startswith(prefix) for prefix in cell["source_paths"])
            for path in changed
        ):
            selected.append(cell)
    return selected


def canary_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One deterministic row per engine/target/format/architecture contract."""
    selected: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for cell in cells:
        coordinate = (
            str(cell["engine"]),
            str(cell["target"]),
            str(cell["format"]),
            str(cell["architecture"]),
        )
        candidate = dict(cell)
        if candidate["engine"] == "build-chain" and candidate.get("canary_tiers"):
            candidate["id"] += "-canary"
            candidate["tiers"] = candidate["canary_tiers"]
        current = selected.get(coordinate)
        if current is None or (candidate.get("canary") and not current.get("canary")):
            selected[coordinate] = candidate
    return sorted(selected.values(), key=lambda cell: cell["id"])


def yaml_at_revision(root: pathlib.Path, revision: str, path: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    value = yaml.safe_load(completed.stdout)
    return value if isinstance(value, dict) else {}


# Which contract keys can change a cell's outcome is decided in ONE place,
# scripts/factory_contract.py, because scripts/tideforge-action-cache.py has
# to answer the same question for its action key (#473). This used to be a
# local table here covering only published_index for deb and pkg.tar.zst —
# added after declaring the served apt indexes re-planned every deb cell,
# a family whose pre-existing breakage then blocked a measurement-only
# change (run 32397627179). The action key had no equivalent, so a bucket
# WRITE path rebuilt every cell on its target.
_pipeline_view = factory_contract.build_view


def changed_contracts(
    root: pathlib.Path, base: str
) -> tuple[set[str], set[str], set[tuple[str, str]]]:
    """Return semantic target/native changes relative to base.

    Missing or unreadable base data fails toward rebuilding every affected
    class by raising; the caller then leaves its selector as ``None``.
    """
    current_factory = load_yaml(root / "manifests/package-factory.yaml")
    old_factory = yaml_at_revision(root, base, "manifests/package-factory.yaml")
    current_targets = current_factory.get("targets") or {}
    old_targets = old_factory.get("targets") or {}
    target_ids = set(current_targets) | set(old_targets)
    changed_targets = {
        target
        for target in target_ids
        if _pipeline_view(current_targets.get(target)) != _pipeline_view(old_targets.get(target))
    }

    current_catalog = current_factory.get("dependency_catalog") or {}
    old_catalog = old_factory.get("dependency_catalog") or {}
    changed_capabilities: set[tuple[str, str]] = set()
    for capability in set(current_catalog) | set(old_catalog):
        current_mapping = current_catalog.get(capability) or {}
        old_mapping = old_catalog.get(capability) or {}
        for target in set(current_mapping) | set(old_mapping):
            if current_mapping.get(target) != old_mapping.get(target):
                changed_capabilities.add((str(capability), str(target)))

    current_registry = load_yaml(root / "manifests/package-builds.yaml")
    old_registry = yaml_at_revision(root, base, "manifests/package-builds.yaml")
    current_rows = {str(row.get("id")): row for row in current_registry.get("native_builds") or []}
    old_rows = {str(row.get("id")): row for row in old_registry.get("native_builds") or []}
    row_ids = set(current_rows) | set(old_rows)
    changed_rows = {row for row in row_ids if current_rows.get(row) != old_rows.get(row)}
    return changed_targets, changed_rows, changed_capabilities


def changed_graph_cells(
    root: pathlib.Path, base: str, cells: list[dict[str, Any]], changed: list[str]
) -> set[str]:
    """Select semantic release-track and target-queue slices."""
    selected: set[str] = set()
    for path in changed:
        if DEPENDENCY_TREE_CHANGE.match(path):
            current = load_yaml(root / path)
            old = yaml_at_revision(root, base, path)
            current_common = {key: value for key, value in current.items() if key != "tracks"}
            old_common = {key: value for key, value in old.items() if key != "tracks"}
            referencing = [cell for cell in cells if cell["dependency_tree"] == path]
            if current_common != old_common:
                selected.update(cell["id"] for cell in referencing)
                continue
            current_tracks = current.get("tracks") or {}
            old_tracks = old.get("tracks") or {}
            changed_tracks = {
                track
                for track in set(current_tracks) | set(old_tracks)
                if current_tracks.get(track) != old_tracks.get(track)
            }
            selected.update(cell["id"] for cell in referencing if cell["track"] in changed_tracks)
        elif TARGET_QUEUE_CHANGE.match(path):
            current = load_yaml(root / path).get("queues") or {}
            old = yaml_at_revision(root, base, path).get("queues") or {}
            changed_targets = {
                target
                for target in set(current) | set(old)
                if current.get(target) != old.get(target)
            }
            selected.update(
                cell["id"]
                for cell in cells
                if cell["target_queue"] == path and cell["target"] in changed_targets
            )
    return selected


def select_by(cells: list[dict[str, Any]], selector: str) -> list[dict[str, Any]]:
    """Filter by a stable cell ID or a data field (``target=``/``family=``)."""
    if not selector:
        return cells
    if "=" not in selector:
        selected = [cell for cell in cells if cell["id"] == selector]
    else:
        field, value = selector.split("=", 1)
        if field not in {"target", "family", "engine", "architecture", "track", "series"} or not value:
            raise ValueError(f"unsupported package factory selector: {selector}")
        selected = [cell for cell in cells if str(cell[field]) == value]
    if not selected:
        raise ValueError(f"package factory selector matched no cells: {selector}")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--changed-files", type=pathlib.Path)
    parser.add_argument("--base", help="base git revision for semantic manifest diffs")
    parser.add_argument("--cell", help="optional exact cell ID for a manual run")
    parser.add_argument("--selector", help="cell ID or target=/family=/engine=/architecture=")
    parser.add_argument("--canary-common", action="store_true")
    parser.add_argument("--github-output", type=pathlib.Path)
    args = parser.parse_args()
    try:
        cells = all_cells(args.root)
        changed = None
        if args.changed_files:
            changed = args.changed_files.read_text(encoding="utf-8").splitlines()
        target_changes = native_changes = capability_changes = graph_changes = None
        if args.base:
            try:
                target_changes, native_changes, capability_changes = changed_contracts(args.root, args.base)
                graph_changes = changed_graph_cells(args.root, args.base, cells, changed or [])
            except (OSError, subprocess.CalledProcessError, ValueError, yaml.YAMLError):
                # Fail toward building all cells in a changed manifest class.
                target_changes = native_changes = capability_changes = graph_changes = None
        selected = select_cells(
            cells,
            changed,
            changed_targets=target_changes,
            changed_native_ids=native_changes,
            changed_capabilities=capability_changes,
            changed_graph_ids=graph_changes,
            canary_common=args.canary_common,
        )
        if args.cell:
            selected = select_by(cells, args.cell)
        if args.selector:
            selected = select_by(selected, args.selector)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"package-factory planner failed closed: {exc}", file=sys.stderr)
        return 2
    shards = [selected[index:index + 200] for index in range(0, len(selected), 200)] or [[]]
    while len(shards) < 3:
        shards.append([])
    if len(shards) > 3:
        print("package-factory planner exceeded three 200-cell shards", file=sys.stderr)
        return 2
    matrices = [json.dumps({"include": shard}, separators=(",", ":")) for shard in shards]
    print(json.dumps({"count": len(selected), "matrices": matrices}))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"count={len(selected)}\n")
            for index, matrix in enumerate(matrices):
                output.write(f"matrix_{index}={matrix}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
