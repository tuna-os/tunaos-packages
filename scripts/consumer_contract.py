"""Immutable image-consumer inputs and conservative factory planning.

A planned provider is build work, never proof that an installation is satisfied.
Native solver/signature/baseline evidence is deliberately kept out of graph guesses.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

MAX_JSON_BYTES = 32 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}\Z")


class ConsumerContractError(ValueError):
    """Consumer identity, coverage or planning input is invalid."""


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ConsumerContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: str | Path) -> dict:
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_JSON_BYTES:
        raise ConsumerContractError(f"invalid or oversized JSON input: {path}")
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ConsumerContractError("non-finite JSON")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConsumerContractError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ConsumerContractError("JSON input must be an object")
    return value


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        raise ConsumerContractError("consumer checkout identity could not be verified") from exc


def _shared(root: Path, revision: str):
    """Load shared validators from the exact verified checkout, under a unique name."""
    name = "_tunaos_consumer_" + revision + "_" + hashlib.sha256(str(root).encode()).hexdigest()[:24]
    directory = root / "scripts/contracts"
    if any(path.suffix in {".pyc", ".pyo"} for path in directory.rglob("*")):
        raise ConsumerContractError("cached bytecode cannot supply pinned shared validators")
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, directory / "__init__.py",
                                                      submodule_search_locations=[str(directory)])
        if spec is None or spec.loader is None:
            raise ConsumerContractError("shared consumer validator is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        previous = sys.dont_write_bytecode
        try:
            sys.dont_write_bytecode = True
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        return importlib.import_module(name + ".evidence"), importlib.import_module(name + ".targets")
    finally:
        sys.dont_write_bytecode = previous


def load_contracts(inputsPath, consumer_root, consumer_revision, required_targetsPath) -> dict:
    root = Path(consumer_root).resolve()
    if not isinstance(consumer_revision, str) or not SHA.fullmatch(consumer_revision):
        raise ConsumerContractError("consumer revision must be an immutable full SHA")
    if _git(root, "rev-parse", "HEAD") != consumer_revision:
        raise ConsumerContractError("consumer checkout does not match requested revision")
    # No dirty shared validators, schemas, manifests or generated coverage may supply trust.
    paths = ["scripts/contracts", "schemas", "required-targets.json", ".github/build-config.yml",
             "manifests", "build_scripts", "Containerfile*"]
    status = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *paths)
    status = "\n".join(line for line in status.splitlines() if "/__pycache__/" not in line and not line.endswith((".pyc", ".pyo")))
    if status:
        raise ConsumerContractError("consumer source material checkout is dirty")
    evidence, targets = _shared(root, consumer_revision)
    inputs, coverage = read_json(inputsPath), read_json(required_targetsPath)
    if type(inputs.get("schemaVersion")) is not int or inputs.get("schemaVersion") != 1 or inputs.get("kind") != "consumer-preparation":
        raise ConsumerContractError("expected version 1 consumer-preparation")
    if type(coverage.get("schemaVersion")) is not int or coverage.get("schemaVersion") != 1 or coverage.get("kind") != "required-targets":
        raise ConsumerContractError("expected version 1 required-targets")
    if inputs.get("sourceRevision") != consumer_revision:
        raise ConsumerContractError("preparation source revision mismatch")
    committed = read_json(root / "required-targets.json")
    if coverage != committed:
        raise ConsumerContractError("required coverage differs from pinned consumer checkout")
    expected = {}
    for row in coverage.get("targets", []):
        key = targets.target_key(row["target"])
        if key in expected:
            raise ConsumerContractError("duplicate required target")
        expected[key] = row
    digest = "sha256:" + hashlib.sha256(evidence.canonical_json(coverage.get("targets"))).hexdigest()
    if coverage.get("coverageDigest") != digest or inputs.get("coverageDigest") != digest:
        raise ConsumerContractError("coverage digest mismatch")
    rows = inputs.get("targets")
    if not isinstance(rows, list):
        raise ConsumerContractError("preparation targets must be an array")
    actual = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ConsumerContractError("preparation target row must be an object")
        key = targets.target_key(row.get("target", {}))
        if key in actual or key not in expected or row["target"] != expected[key]["target"]:
            raise ConsumerContractError("duplicate, unexpected or conflicting target")
        for field in ("required", "scheduled", "publication"):
            if field in {"required", "scheduled"} and type(row.get(field)) is not bool:
                raise ConsumerContractError(f"required target {field} must be a boolean")
            if row.get(field) != expected[key].get(field):
                raise ConsumerContractError(f"required target {field} mismatch")
        contract = row.get("contract")
        if contract is None:
            if row.get("status") != "blocked" or not isinstance(row.get("reasons"), list) or not row["reasons"]:
                raise ConsumerContractError("absent contract requires explicit blocked reasons")
        else:
            try:
                evidence.validate(contract, "consumer-contract")
            except ValueError as exc:
                raise ConsumerContractError(str(exc)) from exc
            if contract["sourceRevision"] != consumer_revision or contract["target"] != row["target"]:
                raise ConsumerContractError("contract source or target mismatch")
            if row.get("status") != contract["resolution"]["status"] or row.get("reasons") != contract["resolution"]["unresolved"]:
                raise ConsumerContractError("preparation resolution mismatch")
        actual[key] = copy.deepcopy(row)
    if actual.keys() != expected.keys():
        raise ConsumerContractError("missing required consumer targets")
    return {"schemaVersion": 1, "consumerRevision": consumer_revision, "coverageDigest": digest,
            "targets": [actual[key] for key in sorted(actual)]}


def _key(target):
    return ":".join(target[field] for field in ("variant", "flavor", "platform"))


def _cycles_and_waves(nodes: set[str], edges: dict[str, set[str]]):
    # Deterministic Tarjan SCCs. Cyclic members are never silently flattened into a wave.
    index, stack, active, indices, low, components = 0, [], set(), {}, {}, []
    def visit(node):
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        active.add(node)
        for child in sorted(edges.get(node, set()) & nodes):
            if child not in indices:
                visit(child)
                low[node] = min(low[node], low[child])
            elif child in active:
                low[node] = min(low[node], indices[child])
        if low[node] == indices[node]:
            component = []
            while True:
                child = stack.pop()
                active.remove(child)
                component.append(child)
                if child == node:
                    break
            components.append(sorted(component))
    for node in sorted(nodes):
        if node not in indices:
            visit(node)
    cycles = sorted(c for c in components if len(c) > 1 or c[0] in edges.get(c[0], set()))
    remaining, done, waves = set(nodes), set(), []
    while remaining:
        wave = sorted(node for node in remaining if edges.get(node, set()) <= done)
        if not wave:
            break
        waves.append(wave)
        remaining.difference_update(wave)
        done.update(wave)
    return waves, cycles, sorted(remaining)


def plan_consumers(contracts: dict, factory: dict, cells: list[dict], provider_catalog: dict | None = None) -> dict:
    catalog = provider_catalog or {}
    providers = catalog.get("providers", [])
    if not isinstance(catalog.get("measurements", []), list):
        raise ConsumerContractError("measurements must be an array")
    if isinstance(providers, dict):
        providers = [{"id": key, **value} for key, value in sorted(providers.items())]
    if not isinstance(providers, list) or any(not isinstance(item, dict) for item in providers):
        raise ConsumerContractError("provider catalog must contain provider objects")
    ids = [item.get("id") for item in providers]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise ConsumerContractError("provider IDs must be unique nonempty strings")
    cells_by_id = {cell["id"]: cell for cell in cells}
    if len(cells_by_id) != len(cells):
        raise ConsumerContractError("duplicate factory cells")
    bindings, graph, rows = {}, {}, []
    provider_graph, provider_cells = {}, {}
    for original in sorted(contracts["targets"], key=lambda row: _key(row["target"])):
        target, contract = original["target"], original.get("contract")
        key = _key(target)
        row = {"targetKey": key, "target": copy.deepcopy(target), "sourceRevision": contracts["consumerRevision"],
               "required": original.get("required"), "scheduled": original.get("scheduled"),
               "contractDigest": contract.get("contractDigest") if contract else None,
               "baseDigest": contract.get("baseDigest") if contract else None,
               "baseReference": contract.get("baseReference") if contract else None,
               "resolution": copy.deepcopy(contract.get("resolution")) if contract else None,
               "packageManager": contract.get("packageManager") if contract else None,
               "baseResolution": copy.deepcopy(contract.get("baseResolution")) if contract else None,
               "sourcePolicy": copy.deepcopy(contract.get("sourcePolicy")) if contract else None,
               "approvedSources": copy.deepcopy(contract.get("approvedSources", [])) if contract else [],
               "demand": copy.deepcopy(contract.get("packageRequirements", [])) if contract else [],
               "plannedProviders": [], "measuredProviders": [], "unresolved": copy.deepcopy(original.get("reasons", [])),
               "queueRoots": [], "readiness": False}
        def gap(code, detail):
            item = {"code": code, "detail": detail}
            if item not in row["unresolved"]:
                row["unresolved"].append(item)
        adapter = factory.get("consumer_adapters", {}).get(target["variant"])
        if not isinstance(adapter, dict):
            gap("missing-consumer-adapter", target["variant"])
            rows.append(row)
            continue
        architecture = adapter.get("architectures", {}).get(target["platform"])
        factory_target = adapter.get("target")
        if not architecture or target["cpuBaseline"] not in adapter.get("cpuBaselines", []):
            gap("unsupported-consumer-platform-baseline", key)
        if factory_target not in factory.get("targets", {}):
            gap("missing-factory-target", str(factory_target))
        queue_roots = factory.get("_consumer_queues", {}).get(factory_target, [])
        if isinstance(queue_roots, dict):
            desktop = target["flavor"].split("-")[0]
            queue_roots = queue_roots.get(desktop, [])
        if not isinstance(queue_roots, list) or any(not isinstance(item, (str, dict)) for item in queue_roots):
            raise ConsumerContractError("consumer queue roots must be native strings or demand objects")
        row["queueRoots"] = copy.deepcopy(queue_roots)
        queue_demands = [{"nativeExpression": item, "name": item} if isinstance(item, str) else item for item in queue_roots]
        if not contract:
            rows.append(row)
            continue
        if adapter.get("manager", contract["packageManager"]) != contract["packageManager"]:
            gap("consumer-manager-mismatch", contract["packageManager"])
        selected, queue = {}, [(demand, None, None) for demand in row["demand"] + queue_demands]
        while queue:
            demand, parent, parent_provider = queue.pop(0)
            expression = demand.get("nativeExpression")
            candidates = [provider for provider in providers
                          if provider.get("target") == factory_target and provider.get("architecture") == architecture
                          and provider.get("cpuBaseline") == target["cpuBaseline"]
                          and provider.get("manager") == contract["packageManager"]
                          and (provider.get("nativeExpression") == expression or
                               (demand.get("name") is not None and provider.get("name") == demand["name"]))]
            if len(candidates) != 1:
                gap("ambiguous-provider" if candidates else "unresolved-provider", str(expression))
                continue
            provider = candidates[0]
            cell_id = provider.get("cellId")
            cell = cells_by_id.get(cell_id)
            if (not cell or cell.get("target") != factory_target or cell.get("architecture") != architecture
                    or cell.get("cpuBaseline", target["cpuBaseline"]) != target["cpuBaseline"]
                    or cell.get("platform", target["platform"]) != target["platform"]):
                gap("provider-cell-target-mismatch", provider["id"])
                continue
            source = provider.get("sourceIdentity")
            source_pinned = isinstance(source, dict) and (
                isinstance(source.get("revision"), str) and SHA.fullmatch(source["revision"]) or
                isinstance(source.get("digest"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", source["digest"]))
            if not source_pinned:
                gap("missing-provider-source-identity", provider["id"])
                continue
            bindings.setdefault(cell_id, set()).add(key)
            graph.setdefault(cell_id, set())
            provider_node = key + "::" + provider["id"]
            provider_cells[provider_node] = cell_id
            provider_graph.setdefault(provider_node, set())
            if parent_provider:
                provider_graph.setdefault(parent_provider, set()).add(provider_node)
            if parent and parent != cell_id:
                graph.setdefault(parent, set()).add(cell_id)
            if demand.get("constraint") or expression != provider.get("name"):
                gap("native-constraint-unproved", str(expression))
            if provider["id"] in selected:
                continue
            selected[provider["id"]] = {**copy.deepcopy(provider), "readiness": False}
            for phase in ("sourceDependencies", "buildDependencies", "runtimeDependencies"):
                dependencies = provider.get(phase, [])
                if phase != "sourceDependencies" and phase not in provider:
                    gap("unmeasured-provider-dependency-closure", provider["id"] + ":" + phase)
                if not isinstance(dependencies, list) or any(not isinstance(item, dict) or not isinstance(item.get("nativeExpression"), str) for item in dependencies):
                    gap("invalid-provider-dependencies", provider["id"] + ":" + phase)
                    continue
                queue.extend((dependency, cell_id, provider_node) for dependency in dependencies)
        row["plannedProviders"] = [selected[value] for value in sorted(selected)]
        # Measurements must bind the complete consumer identity. They remain untrusted
        # observations here; native transaction/signature validation belongs to verification.
        for measurement in catalog.get("measurements", []):
            if not isinstance(measurement, dict):
                raise ConsumerContractError("measurement must be an object")
            if measurement.get("target") != target:
                continue
            required_identity = {"sourceRevision": row["sourceRevision"], "contractDigest": row["contractDigest"],
                                 "baseDigest": row["baseDigest"]}
            if any(measurement.get(field) != value for field, value in required_identity.items()):
                gap("measured-provider-identity-mismatch", key)
                continue
            refs = measurement.get("evidence")
            if not isinstance(refs, list) or not refs or any(
                    not isinstance(ref, dict) or not isinstance(ref.get("digest"), str) or
                    not re.fullmatch(r"sha256:[0-9a-f]{64}", ref["digest"]) for ref in refs):
                gap("missing-measured-provider-evidence", key)
                continue
            row["measuredProviders"].append({**copy.deepcopy(measurement), "readiness": False, "verified": False})
        if selected:
            gap("native-provider-verification-required", key)
        row["unresolved"] = sorted(row["unresolved"], key=lambda item: json.dumps(item, sort_keys=True))
        rows.append(row)
    provider_waves, provider_cycles, provider_blocked = _cycles_and_waves(set(provider_graph), provider_graph)
    provider_blocked_cells = {provider_cells[node] for node in provider_blocked}
    waves, cycles, blocked = _cycles_and_waves(set(bindings), graph)
    blocked = sorted(set(blocked) | provider_blocked_cells)
    # Real provider cycles cannot be made runnable by collapsing family cells.
    waves = [[cell for cell in wave if cell not in provider_blocked_cells] for wave in waves]
    waves = [wave for wave in waves if wave]
    for row in rows:
        key = _key(row["target"])
        if any(key in bindings.get(cell, set()) for cell in blocked):
            row["unresolved"].append({"code": "dependency-cycle-or-blocked-wave", "detail": key})
    normalized = {key: sorted(value) for key, value in sorted(bindings.items())}
    return {"schemaVersion": 1, "kind": "consumer-factory-plan", "consumerRevision": contracts["consumerRevision"],
            "coverageDigest": contracts.get("coverageDigest"), "consumers": rows, "cellBindings": normalized,
            "affectedConsumers": copy.deepcopy(normalized),
            "dependencyCells": {cell: sorted(dependencies) for cell, dependencies in sorted(graph.items())},
            "providerWaves": provider_waves, "providerCycles": provider_cycles,
            "waves": waves, "cycles": cycles,
            "blockedCells": blocked, "readiness": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consumer-root", required=True)
    parser.add_argument("--consumer-revision", required=True)
    parser.add_argument("--contracts", required=True)
    parser.add_argument("--required-targets", required=True)
    parser.add_argument("--factory", required=True, help="JSON factory mapping")
    parser.add_argument("--cells", required=True, help="JSON object containing cells array")
    parser.add_argument("--provider-catalog")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        contracts = load_contracts(args.contracts, args.consumer_root, args.consumer_revision, args.required_targets)
        report = plan_consumers(contracts, read_json(args.factory), read_json(args.cells)["cells"],
                                read_json(args.provider_catalog) if args.provider_catalog else None)
        Path(args.output).write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    except (ConsumerContractError, OSError, KeyError, ValueError) as exc:
        print(f"consumer contract error: {exc}", file=sys.stderr)
        return 2
    return 0
