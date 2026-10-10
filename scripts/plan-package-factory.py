#!/usr/bin/env python3
"""Emit the one package-factory matrix from recipes and native queue data."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from typing import Any

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from target_platform import build_context
import factory_contract  # noqa: E402  (needs the path above)
import consumer_contract


RECIPE_CHANGE = re.compile(r"^packages/([^/]+)/")
COMMON_INPUTS = {
    ".github/workflows/package-factory.yml",
    ".github/workflows/package-factory-cell.yml",
    ".github/actions/tideforge-action-cache/action.yml",
    "scripts/run-package-factory-cell.sh",
    "scripts/verify-package-factory-cell.sh",
    "scripts/tideforge-action-cache.py",
    "scripts/tideforge.py",
    "scripts/target_platform.py",
    "scripts/run-alma-rpm.sh",
    "scripts/alma-compiler-policy.sh",
    "scripts/factory-batches.py",
}
FORMAT_INPUTS = {
    "scripts/assemble-deb-source-tree.py": {"deb"},
    "scripts/arch-clean-install.sh": {"pkg.tar.zst"},
    "scripts/arch-native-policy.sh": {"pkg.tar.zst"},
    "scripts/arch-verify-published.sh": {"pkg.tar.zst"},
}
NATIVE_INPUTS = {"scripts/build-chain.sh", "scripts/parse-build-order.py", "scripts/candidate-rpm-repository.py", "scripts/alma-rpmbuild-guard.py",
                 "scripts/alma-candidate-snapshot.py", "scripts/alma-candidate-resume.py", "scripts/github_api.py"}
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
            package_format = target.get("format")
            if not package_format:
                raise ValueError(f"{recipe_path}: incomplete target contract {target_id}")
            architectures = target.get("architectures") or []
            for architecture in architectures:
                context = build_context(target, str(architecture))
                image = context["image"]
                cells.append(
                    {
                        "id": factory_contract.tideforge_cell_id(package, target_id, architecture),
                        "engine": "tideforge",
                        "package": package,
                        "recipe": recipe_path.relative_to(root).as_posix(),
                        "target": target_id,
                        "format": package_format,
                        "architecture": architecture,
                        **context,
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
        if cell["target"] in {"alma10", "alma10-kitten"}:
            factory = load_yaml(root / "manifests/package-factory.yaml")
            target = factory["targets"][cell["target"]]
            context = build_context(target, cell["architecture"])
            if any(cell.get(key) != value for key, value in (
                ("verify_image", context["image"]), ("platform", context["platform"]),
                ("cpu_baseline", context["cpu_baseline"]),
            )):
                raise ValueError(f"native Alma build {cell['id']} has incompatible verification context")
            family, name, architecture = cell["family"], cell["target"], cell["architecture"]
            suffix = "-aarch64" if architecture == "aarch64" else ""
            if (cell["mock_config"] != f"{name}-ci-{family}{suffix}"
                    or cell.get("r2_path") != f"{family}/{name}-{architecture}"):
                raise ValueError(f"native Alma build {cell['id']} has incompatible namespace or mock config")
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


def declared_consumer_providers(root: pathlib.Path, factory: dict, cells: list[dict]) -> dict:
    """Recipe declarations provide build work; native receipts prove satisfaction."""
    providers = []
    for cell in cells:
        if cell["engine"] != "tideforge":
            continue
        path = root / cell["recipe"]
        recipe = load_yaml(path)
        manager = {"rpm": "dnf", "deb": "apt", "pkg.tar.zst": "pacman"}.get(cell["format"])
        matching = [adapter for adapter in factory.get("consumer_adapters", {}).values()
                    if adapter.get("target") == cell["target"]]
        managers = {adapter.get("manager", manager) for adapter in matching}
        if len(managers) > 1:
            raise ValueError("factory target maps to conflicting native consumer managers")
        manager = next(iter(managers), manager)
        baselines = {baseline for adapter in matching for platform, arch in adapter.get("architectures", {}).items()
                     if arch == cell["architecture"] for baseline in adapter.get("cpuBaselines", [])
                     if (baseline == "armv8-a") == (platform == "linux/arm64")}
        for baseline in sorted(baselines):
            provider = {"id": cell["id"] + ":" + baseline, "name": recipe["name"],
                        "nativeExpression": recipe["name"], "manager": manager,
                        "target": cell["target"], "architecture": cell["architecture"],
                        "cpuBaseline": baseline, "cellId": cell["id"],
                        "sourceIdentity": {"path": cell["recipe"], "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()},
                        "declaredVersion": str(recipe.get("version", "")), "readiness": False}
            for phase, output in (("build", "buildDependencies"), ("runtime", "runtimeDependencies")):
                dependencies = (recipe.get("dependencies") or {}).get(phase) or {}
                expressions = list(dependencies.get("common") or []) + list((dependencies.get("targets") or {}).get(cell["target"]) or [])
                for capability in dependencies.get("capabilities") or []:
                    mapping = (factory.get("dependency_catalog") or {}).get(capability, {}).get(cell["target"])
                    expressions.extend(mapping if isinstance(mapping, list) else ["unmapped-capability:" + capability])
                provider[output] = [{"nativeExpression": expression} for expression in expressions]
            providers.append(provider)
    return {"providers": providers}


def consumer_factory_plan(args: argparse.Namespace, cells: list[dict]) -> dict | None:
    inputs = [args.consumer_contracts, args.consumer_root, args.consumer_revision, args.consumer_required_targets]
    if not any(inputs):
        if args.consumer_provider_catalog or args.consumer_plan_output:
            raise ValueError("consumer inputs are required for a provider catalog or plan output")
        return None
    if not all(inputs):
        raise ValueError("consumer contracts, root, immutable revision and required coverage must be supplied together")
    contracts = consumer_contract.load_contracts(*[args.consumer_contracts, args.consumer_root,
                                                  args.consumer_revision, args.consumer_required_targets])
    factory = copy.deepcopy(load_yaml(args.root / "manifests/package-factory.yaml"))
    queues = {}
    for path in sorted((args.root / "manifests/target-queues").glob("*.yaml")):
        for target, queue in (load_yaml(path).get("queues") or {}).items():
            queues.setdefault(target, {})[path.stem] = queue.get("roots", [])
    factory["_consumer_queues"] = queues
    catalog = (consumer_contract.read_json(args.consumer_provider_catalog) if args.consumer_provider_catalog
               else declared_consumer_providers(args.root, factory, cells))
    report = consumer_contract.plan_consumers(contracts, factory, cells, catalog)
    if args.consumer_plan_output:
        args.consumer_plan_output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return report


def bind_consumer_cells(cells: list[dict], selected: list[dict], report: dict) -> list[dict]:
    """Rebuild dependents, keeping unrelated cells and consumers out of the selection."""
    by_id = {cell["id"]: cell for cell in cells}
    selected_by_id = {cell["id"]: copy.deepcopy(cell) for cell in selected}
    affected = {cell.get("base_id", cell["id"].removesuffix("-canary")) for cell in selected}
    changed = True
    while changed:
        changed = False
        for parent, dependencies in report["dependencyCells"].items():
            if set(dependencies) & affected and parent not in affected:
                affected.add(parent)
                if parent in by_id:
                    selected_by_id[parent] = copy.deepcopy(by_id[parent])
                changed = True
    consumers = {row["targetKey"]: row for row in report["consumers"]}
    for cell in selected_by_id.values():
        identity = cell.get("base_id", cell["id"].removesuffix("-canary"))
        bindings = []
        for key in report["cellBindings"].get(identity, []):
            row = consumers[key]
            bindings.append({field: row[field] for field in ("target", "sourceRevision", "contractDigest", "baseDigest", "baseReference", "approvedSources")})
        if bindings:
            cell["consumer_bindings"] = bindings
    return sorted(selected_by_id.values(), key=lambda cell: cell["id"])


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
    # Cells pulled in ONLY because a renderer script moved -- build-chain.sh,
    # parse-build-order.py, import-fedora-distgit.py. Nothing about the
    # packages themselves changed, so on a PR these are bounded to their first
    # tier rather than rebuilding every family end to end. See
    # bound_to_canary_tiers() for why that is the right depth.
    renderer_only = []
    for cell in cells:
        if cell["engine"] == "tideforge" and cell["package"] in changed_packages:
            selected.append(cell)
            continue
        if formats and cell["format"] in formats:
            selected.append(cell)
            continue
        # Checked BEFORE the renderer rules: a cell whose own manifest or
        # sources moved must build for real, however it was also selected.
        if cell["engine"] == "build-chain" and any(
            path == cell["manifest"] or any(path.startswith(prefix) for prefix in cell["source_paths"])
            for path in changed
        ):
            selected.append(cell)
            continue
        if changed & NATIVE_INPUTS and cell["engine"] == "build-chain":
            renderer_only.append(cell)
            continue
        if changed & DISTGIT_INPUTS and cell["engine"] == "build-chain" and cell["uses_distgit"]:
            renderer_only.append(cell)
    if canary_common:
        renderer_only = bound_to_canary_tiers(renderer_only)
    return selected + renderer_only


def bound_to_canary_tiers(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same cells, each bounded to the FIRST tier of its own chain.

    For a renderer-script change there is no package to attribute the edit to,
    so every build-chain family gets selected and each one rebuilds its entire
    chain. Measured on #576's gate, a two-line build-chain.sh fix pulled in 36
    cells: xfce-el10-x86_64 took 51m, xfce-el10-aarch64 38m, and gnome50/51 ran
    past 50m without finishing -- for a change that could not alter what any of
    those packages compile to.

    This is deliberately NOT canary_cells(). That collapses to one cell per
    (engine, target, format, architecture), and every el10 rpm family shares
    that coordinate -- gnome50, gnome51, xfce and fprintd would become a single
    representative, so a renderer bug that only bites the gnome51 chain would
    sail through. Keeping every family and cutting the DEPTH preserves the
    coverage that matters here while removing the hours.

    Setting `tiers` also drops the -c1/-c2 continuations, which exist to resume
    a long chain and have nothing to resume from a single tier.

    The nightly, the publish legs and the fan-out never pass canary_common, so
    they are unaffected and still build every family end to end.
    """
    bounded = []
    for cell in cells:
        candidate = dict(cell)
        tiers = candidate.get("canary_tiers")
        if tiers:
            candidate["id"] += "-canary"
            candidate["tiers"] = tiers
        bounded.append(candidate)
    return bounded


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


def selection_batches(selected: list[dict], source_revision: str) -> dict:
    """Reserve continuation capacity without discarding any selected cell."""
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("batch source revision must be a full immutable SHA")
    identities = [cell["id"] for cell in selected]
    if len(identities) != len(set(identities)):
        raise ValueError("batch selection has duplicate cell IDs")
    def full(cell):
        return cell.get("engine") == "build-chain" and not cell.get("tiers") and not cell.get("canary")
    canonical = json.dumps({"sourceRevision": source_revision, "cells": selected},
                           sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    identity = "sha256:" + hashlib.sha256(canonical).hexdigest()
    original = [selected[index:index + 200] for index in range(0, len(selected), 200)] or [[]]
    while len(original) < 3:
        original.append([])
    continuations = sum(full(cell) for cell in original[0])
    if len(original) == 3 and all(len(original[index]) + continuations <= 200 for index in (1, 2)):
        batches = [original]
    else:
        native = [cell for cell in selected if full(cell)]
        recipes = [cell for cell in selected if not full(cell)]
        batches = []
        while native or recipes:
            first = native[:200]
            native = native[200:]
            reserved = len(first)
            capacities = [200 - reserved, 200 - reserved, 200 - reserved]
            shards = [first, [], []]
            for index, capacity in enumerate(capacities):
                shards[index].extend(recipes[:capacity])
                recipes = recipes[capacity:]
            batches.append(shards)
    return {"selection_digest": identity, "shards": batches,
            "planned_batches": [{"batch_index": index, "cells": [cell["id"] for shard in batch for cell in shard]}
                                for index, batch in enumerate(batches)]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--changed-files", type=pathlib.Path)
    parser.add_argument("--base", help="base git revision for semantic manifest diffs")
    parser.add_argument("--cell", help="optional exact cell ID for a manual run")
    parser.add_argument("--selector", help="cell ID or target=/family=/engine=/architecture=")
    parser.add_argument("--canary-common", action="store_true")
    parser.add_argument("--batch-index", type=int, default=0)
    parser.add_argument("--selection-digest")
    parser.add_argument("--github-output", type=pathlib.Path)
    parser.add_argument("--consumer-contracts", type=pathlib.Path)
    parser.add_argument("--consumer-root", type=pathlib.Path)
    parser.add_argument("--consumer-revision")
    parser.add_argument("--consumer-required-targets", type=pathlib.Path)
    parser.add_argument("--consumer-provider-catalog", type=pathlib.Path)
    parser.add_argument("--consumer-plan-output", type=pathlib.Path)
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
        consumer_plan = consumer_factory_plan(args, cells)
        if consumer_plan is not None:
            selected = bind_consumer_cells(cells, selected, consumer_plan)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"package-factory planner failed closed: {exc}", file=sys.stderr)
        return 2
    try:
        source_revision = subprocess.run(["git", "-C", str(args.root), "rev-parse", "HEAD"],
                                         check=True, capture_output=True, text=True).stdout.strip()
        batch_plan = selection_batches(selected, source_revision)
        if args.batch_index < 0 or args.batch_index >= len(batch_plan["shards"]):
            raise ValueError("batch index outside complete selection")
        if args.selection_digest is not None and args.selection_digest != batch_plan["selection_digest"]:
            raise ValueError("selection digest does not match pinned source and inventory")
    except (ValueError, subprocess.SubprocessError) as exc:
        print(f"package-factory batching failed closed: {exc}", file=sys.stderr)
        return 2
    selection = selected
    shards = batch_plan["shards"][args.batch_index]
    selected = [cell for shard in shards for cell in shard]

    # Continuation shards for full-chain build-chain cells.
    #
    # A whole desktop family (1,248 packages for hummingbird) does not fit one
    # job: the soft deadline (CHAIN_BUDGET_SECONDS) ends the cell cleanly at
    # ~4.5h with the rest DEFERRED, and before this the next attempt was
    # tomorrow's schedule -- ~5.5h of chain per day, weeks to converge. The
    # build-1/build-2 shards existed only as >200-cell overflow and had never
    # run. Chaining them (build-1 needs build-0, build-2 needs build-1) and
    # seeding them with CONTINUATION copies of each full-chain cell triples
    # the chain hours per run using only mechanisms that already exist:
    #
    #   * the continuation carries base_id = the original cell id; the
    #     partial artifact is NAMED by base_id, so the resume step restores
    #     the previous shard's output (same-run artifacts are visible to the
    #     API once finalized -- pinned by an existing test);
    #   * the action key is derived from inputs, which are identical, so if
    #     an earlier shard FINISHED the chain and recorded its ActionResult,
    #     the continuation cache-hits and skips the build entirely;
    #   * a continuation that runs out of budget defers again, uploads a
    #     bigger partial under the same base_id name (overwrite), and the
    #     cache stays unwritten -- exactly the single-shard semantics.
    #
    # Only full-chain cells continue: a TIERS-scoped dispatch asked for a
    # bounded slice, and tideforge cells build one package with no partial
    # progress to resume. Canary cells (canary=true) are bounded by
    # construction and excluded for the same reason.
    def _continuation(cell: dict, suffix: str) -> dict:
        cont = dict(cell)
        cont["base_id"] = cell.get("base_id") or cell["id"]
        cont["id"] = f"{cell['id']}-{suffix}"
        return cont

    full_chain = [
        cell for cell in shards[0]
        if cell.get("engine") == "build-chain"
        and not cell.get("tiers")
        and not cell.get("canary")
    ]
    for shard_index, suffix in ((1, "c1"), (2, "c2")):
        for cell in full_chain:
            if len(shards[shard_index]) >= 200:
                print("package-factory planner: continuation shard is full; "
                      "dropping would silently halve chain throughput",
                      file=sys.stderr)
                return 2
            shards[shard_index].append(_continuation(cell, suffix))

    matrices = [json.dumps({"include": shard}, separators=(",", ":")) for shard in shards]
    pending = [batch for batch in batch_plan["planned_batches"] if batch["batch_index"] > args.batch_index]
    result = {"count": len(selected), "matrices": matrices,
              "batch_index": args.batch_index, "batch_count": len(batch_plan["shards"]),
              "source_revision": source_revision, "selection_count": len(selection),
              "selection_digest": batch_plan["selection_digest"], "selection_inventory": selection,
              "planned_batches": batch_plan["planned_batches"], "pending_batches": pending,
              "pending_cells": [cell for batch in pending for cell in batch["cells"]],
              "readiness": False}

    if consumer_plan is not None:
        result["consumerPlan"] = consumer_plan
    print(json.dumps(result))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"count={len(selected)}\n")
            for field in ("batch_index", "batch_count", "selection_count", "selection_digest", "source_revision"):
                output.write(f"{field}={result[field]}\n")
            for field in ("pending_cells", "pending_batches", "planned_batches"):
                output.write(f"{field}={json.dumps(result[field], separators=(',', ':'))}\n")
            for index, matrix in enumerate(matrices):
                output.write(f"matrix_{index}={matrix}\n")
                # Shard occupancy drives the build-1/build-2 job conditions:
                # ">200 cells" stopped being the only reason a later shard
                # has work the moment continuations existed.
                output.write(f"count_{index}={len(shards[index])}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
