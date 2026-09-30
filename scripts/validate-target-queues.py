#!/usr/bin/env python3
"""Validate desktop target queues against their shared dependency trees."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

IMPLEMENTATION_FORMATS = {
    "native-spec": {"rpm"},
    "tideforge-rpm": {"rpm"},
    "tideforge-debian": {"deb"},
    "native-pkgbuild": {"pkg.tar.zst"},
    "tideforge-pkgbuild": {"pkg.tar.zst"},
}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        fail(f"{path}: {error}")
    if not isinstance(data, dict):
        fail(f"{path}: expected a YAML mapping")
    return data


def validate_queue(queue_path: Path, tree_path: Path, repository: Path) -> None:
    queue_data = load_yaml(queue_path)
    tree_data = load_yaml(tree_path)
    if queue_data.get("schema") != 1:
        fail(f"{queue_path}: schema must be 1")
    if tree_data.get("schema") != 1:
        fail(f"{tree_path}: schema must be 1")

    queues = queue_data.get("queues")
    if not isinstance(queues, dict) or not queues:
        fail(f"{queue_path}: queues must be a non-empty mapping")
    tree_targets = set(tree_data.get("targets", []))
    if not tree_targets:
        fail(f"{tree_path}: targets must be non-empty")
    missing_targets = tree_targets - set(queues)
    if missing_targets:
        fail(f"{queue_path}: missing queues for tree targets: {sorted(missing_targets)}")

    nodes = set(tree_data.get("nodes", {}))
    for target, queue in queues.items():
        if not isinstance(queue, dict):
            fail(f"{queue_path}: {target} must be a mapping")
        package_format = queue.get("format")
        implementation = queue.get("implementation")
        if implementation not in IMPLEMENTATION_FORMATS:
            fail(f"{queue_path}: {target} has unknown implementation {implementation!r}")
        if package_format not in IMPLEMENTATION_FORMATS[implementation]:
            fail(f"{queue_path}: {target} format {package_format!r} is incompatible with {implementation}")
        gates = queue.get("gates")
        if not isinstance(gates, list) or not gates or not all(isinstance(gate, str) and gate for gate in gates):
            fail(f"{queue_path}: {target} must define non-empty gates")
        if package_format == "deb" and not isinstance(queue.get("suite"), str):
            fail(f"{queue_path}: {target} DEB queue must define a suite")

        build_order = queue.get("build_order")
        if build_order:
            build_order_path = repository / build_order
            if not build_order_path.is_file():
                fail(f"{queue_path}: {target} build_order does not exist: {build_order}")

        roots = queue.get("roots", [])
        if not isinstance(roots, list) or not all(isinstance(root, str) and root for root in roots):
            fail(f"{queue_path}: {target} roots must be a list of package names")
        # A native build-order may legitimately contain packaging-only roots;
        # otherwise roots must belong to the shared source graph.
        if not build_order:
            unknown_roots = set(roots) - nodes
            if unknown_roots:
                fail(f"{queue_path}: {target} roots are absent from {tree_path.name}: {sorted(unknown_roots)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queues", type=Path, default=Path("manifests/target-queues"))
    parser.add_argument("--trees", type=Path, default=Path("manifests/dependency-trees"))
    parser.add_argument("--repository", type=Path, default=Path("."))
    args = parser.parse_args()

    queue_paths = sorted(args.queues.glob("*.yaml"))
    if not queue_paths:
        fail(f"no queue manifests found in {args.queues}")
    for queue_path in queue_paths:
        tree_path = args.trees / queue_path.name
        if not tree_path.is_file():
            fail(f"{queue_path}: matching dependency tree is missing: {tree_path}")
        validate_queue(queue_path, tree_path, args.repository)
    print(f"Target queues: valid ({len(queue_paths)} manifests)")


if __name__ == "__main__":
    main()
