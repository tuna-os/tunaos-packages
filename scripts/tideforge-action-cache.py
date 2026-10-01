#!/usr/bin/env python3
"""Create Tideforge action keys and verified package result manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any, Iterable

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import factory_contract  # noqa: E402  (needs the path above)


SCHEMA = 1
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
COMMON_RENDERERS = (
    "scripts/tideforge.py",
    "scripts/run-package-factory-cell.sh",
    "scripts/fetch-tideforge-sources.py",
)
FORMAT_RENDERERS = {
    "deb": ("scripts/assemble-deb-source-tree.py",),
    "rpm": (),
    "pkg.tar.zst": (),
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_file(path: pathlib.Path) -> str:
    return digest_bytes(path.read_bytes())


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_json(value))


def digest_tree(root: pathlib.Path) -> str:
    if not root.is_dir():
        raise SystemExit(f"recipe directory does not exist: {root}")
    entries = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        if ".git" in path.parts:
            continue
        entries.append({"path": path.relative_to(root).as_posix(), "digest": digest_file(path)})
    return digest_json(entries)


def digest_path(path: pathlib.Path) -> str:
    if path.is_file():
        return digest_file(path)
    return digest_tree(path)


def require_sha256(value: str, label: str = "digest") -> str:
    if not SHA256.fullmatch(value):
        raise SystemExit(f"{label} must be sha256:<64 lowercase hexadecimal characters>")
    return value


def require_image_digest(image: str) -> str:
    if not IMAGE_DIGEST.fullmatch(image):
        raise SystemExit("build image must be immutable and digest-pinned (image@sha256:...)")
    return image


def load_factory(path: pathlib.Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("package factory must contain a mapping")
    return data


def target_inputs(
    factory: dict[str, Any], target_id: str, capabilities: Iterable[str] = ()
) -> dict[str, Any]:
    try:
        target = factory["targets"][target_id]
    except (KeyError, TypeError) as exc:
        raise SystemExit(f"unknown factory target: {target_id}") from exc
    if not isinstance(target, dict):
        raise SystemExit(f"factory target {target_id} must contain a mapping")

    resolved = {}
    catalog = factory.get("dependency_catalog") or {}
    for name in sorted(set(capabilities)):
        targets = catalog.get(name)
        if not isinstance(targets, dict) or target_id not in targets:
            raise SystemExit(f"dependency capability {name} has no mapping for {target_id}")
        resolved[name] = targets[target_id]
    # The BUILD's view of the contract, not the whole block (#473). Hashing
    # every field meant a bucket write path or a reporting label rebuilt
    # every cell on the target from scratch. Same view the planner selects
    # with, so the two cannot drift.
    return {
        "contract": factory_contract.build_view(target),
        "dependency_capabilities": resolved,
    }


def recipe_capabilities(recipe: dict[str, Any]) -> list[str]:
    dependencies = recipe.get("dependencies") or {}
    return [
        str(capability)
        for phase in ("build", "runtime")
        for capability in ((dependencies.get(phase) or {}).get("capabilities") or [])
    ]


def renderer_paths(target: dict[str, Any]) -> tuple[str, ...]:
    package_format = target.get("format")
    if package_format not in FORMAT_RENDERERS:
        raise SystemExit(f"unsupported target package format: {package_format}")
    return COMMON_RENDERERS + FORMAT_RENDERERS[package_format]


def relative_recipe_path(recipe: pathlib.Path, root: pathlib.Path) -> str:
    try:
        return recipe.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise SystemExit(f"recipe must be inside repository root: {recipe}") from exc


def action_inputs(args: argparse.Namespace) -> dict[str, Any]:
    root = pathlib.Path(args.root)
    recipe = pathlib.Path(args.recipe)
    factory = load_factory(pathlib.Path(args.factory))
    recipe_data = load_factory(recipe)
    selected = target_inputs(factory, args.target, recipe_capabilities(recipe_data))
    target = selected["contract"]
    if args.arch not in target.get("architectures", []):
        raise SystemExit(f"{args.arch} is not declared for target {args.target}")
    if int(args.source_date_epoch) <= 0:
        raise SystemExit("SOURCE_DATE_EPOCH must be a positive integer")

    renderers = renderer_paths(target)
    renderer_digests = {}
    for relative in renderers:
        path = root / relative
        if not path.is_file():
            raise SystemExit(f"renderer input does not exist: {relative}")
        renderer_digests[relative] = digest_file(path)

    dependency_keys = sorted(set(args.dependency_key))
    for key in dependency_keys:
        require_sha256(key, "dependency action key")

    return {
        "schema": SCHEMA,
        "recipe": {
            "path": relative_recipe_path(recipe, root),
            "tree": digest_tree(recipe.parent),
        },
        "target": {
            "id": args.target,
            "architecture": args.arch,
            "inputs": selected,
        },
        "build_image": require_image_digest(args.image),
        "renderer_inputs": renderer_digests,
        "dependency_action_keys": dependency_keys,
        "reproducibility": {
            "contract": 1,
            "source_date_epoch": int(args.source_date_epoch),
        },
    }


def native_action_inputs(args: argparse.Namespace) -> dict[str, Any]:
    root = pathlib.Path(args.root)
    factory = load_factory(pathlib.Path(args.factory))
    # Native build-order manifests do not consume Tideforge's dependency
    # capability catalog, so catalog edits must not invalidate native queues.
    selected = target_inputs(factory, args.target)
    target = selected["contract"]
    if args.arch not in target.get("architectures", []):
        raise SystemExit(f"{args.arch} is not declared for target {args.target}")
    if int(args.source_date_epoch) <= 0:
        raise SystemExit("SOURCE_DATE_EPOCH must be a positive integer")
    inputs = []
    # The manifest is hashed as THIS TARGET'S SLICE, not as a file, exactly as
    # the target_queue and dependency_tree contracts below already are.
    #
    # It used to be `digest_path(manifest)` -- the whole file, every target --
    # which silently defeated #473. That change narrowed the manifest's
    # contribution to `build_view(target)` precisely so a field no build reads
    # could not rebuild a cell, and then the whole-file digest put every other
    # target's fields straight back into the key.
    #
    # Measured cost, run 32842254545: #512 edited this manifest to add `drift:`
    # blocks and correct `probe_image` -- neither of which changes what any
    # hummingbird RPM compiles to. The key moved, a 429 MB partial written 34
    # seconds earlier was rejected as "action key differs", and a 22-tier chain
    # restarted at bootstrap-00. Four hours of runner time reached layer-00.
    # See #528.
    #
    # `schema` rides along because it decides how every other field is read; a
    # schema bump must still invalidate. What is dropped is other targets'
    # blocks and `upstreams`, plus `dependency_catalog`, which native targets
    # are already documented above as not consuming.
    for raw in [*args.input]:
        path = pathlib.Path(raw)
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError as exc:
            raise SystemExit(f"native input must be inside repository root: {path}") from exc
        inputs.append({"path": relative, "digest": digest_path(path)})
    manifest_path = pathlib.Path(args.manifest)
    try:
        manifest_relative = manifest_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise SystemExit(f"native input must be inside repository root: {manifest_path}") from exc
    inputs.append({
        "path": manifest_relative,
        "digest": digest_json({"schema": factory.get("schema"), "target": selected}),
    })
    contracts = []
    if args.dependency_tree:
        path = pathlib.Path(args.dependency_tree)
        data = load_factory(path)
        tracks = data.pop("tracks", {}) or {}
        if args.track not in tracks:
            raise SystemExit(f"dependency tree has no release track {args.track}")
        contracts.append(
            {
                "path": path.resolve().relative_to(root.resolve()).as_posix(),
                "digest": digest_json({"common": data, "track": tracks[args.track]}),
            }
        )
    if args.target_queue:
        path = pathlib.Path(args.target_queue)
        data = load_factory(path)
        queues = data.get("queues") or {}
        if args.target not in queues:
            raise SystemExit(f"target queue has no target {args.target}")
        contracts.append(
            {
                "path": path.resolve().relative_to(root.resolve()).as_posix(),
                "digest": digest_json({"schema": data.get("schema"), "queue": queues[args.target]}),
            }
        )
    native_renderers = ["scripts/build-chain.sh", "scripts/run-package-factory-cell.sh"]
    if "distgit:" in pathlib.Path(args.manifest).read_text(encoding="utf-8"):
        native_renderers.append("scripts/import-fedora-distgit.py")
    renderer_inputs = {}
    for relative in native_renderers:
        path = root / relative
        if not path.is_file():
            raise SystemExit(f"renderer input does not exist: {relative}")
        renderer_inputs[relative] = digest_file(path)
    return {
        "schema": SCHEMA,
        "identity": args.identity,
        "native_inputs": sorted(inputs, key=lambda entry: entry["path"]),
        "native_contracts": sorted(contracts, key=lambda entry: entry["path"]),
        "release_track": {"name": args.track, "series": args.series},
        "target": {"id": args.target, "architecture": args.arch, "inputs": selected},
        "build_image": require_image_digest(args.image),
        "renderer_inputs": renderer_inputs,
        "dependency_action_keys": [],
        "reproducibility": {"contract": 1, "source_date_epoch": int(args.source_date_epoch)},
    }


def action_key(inputs: dict[str, Any]) -> str:
    return digest_json(inputs)


def safe_artifact_name(name: str) -> str:
    if not name or name in {".", ".."} or pathlib.PurePath(name).name != name:
        raise SystemExit(f"unsafe artifact name in result: {name!r}")
    return name


def artifact(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"artifact is not a regular file: {path}")
    return {"name": safe_artifact_name(path.name), "size": path.stat().st_size, "digest": digest_file(path)}


def create_result(key: str, paths: Iterable[pathlib.Path]) -> dict[str, Any]:
    require_sha256(key, "action key")
    artifacts = [artifact(path) for path in paths]
    if not artifacts:
        raise SystemExit("an ActionResult must contain at least one artifact")
    names = [entry["name"] for entry in artifacts]
    if len(names) != len(set(names)):
        raise SystemExit("artifact names must be unique within an ActionResult")
    return {"schema": SCHEMA, "action_key": key, "artifacts": artifacts}


def load_quarantine(path: pathlib.Path | None = None) -> list[dict[str, Any]]:
    manifest = path or (pathlib.Path("manifests/reproducibility-quarantine.yaml"))
    if not manifest.is_file():
        return []
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        entries = data.get("quarantined_packages")
        return entries if isinstance(entries, list) else []
    except (yaml.YAMLError, OSError):
        return []


def is_quarantined(
    package: str,
    target: str | None = None,
    arch: str | None = None,
    path: pathlib.Path | None = None,
) -> bool:
    for entry in load_quarantine(path):
        if not isinstance(entry, dict) or entry.get("package") != package:
            continue
        if target and entry.get("target") and entry.get("target") != target:
            continue
        if arch and entry.get("architecture") and entry.get("architecture") != arch:
            continue
        return True
    return False


def verify_result(
    result: dict[str, Any],
    artifact_dir: pathlib.Path,
    expected_key: str | None = None,
    quarantine_manifest: pathlib.Path | None = None,
    package: str | None = None,
    target: str | None = None,
    arch: str | None = None,
) -> None:
    if result.get("schema") != SCHEMA:
        raise SystemExit("unsupported ActionResult schema")
    key = require_sha256(str(result.get("action_key", "")), "action key")
    if expected_key and key != require_sha256(expected_key, "expected action key"):
        raise SystemExit("ActionResult key does not match the requested action")
    if package and is_quarantined(package, target, arch, quarantine_manifest):
        raise SystemExit(f"recipe is quarantined due to reproducibility divergence: {package}")
    entries = result.get("artifacts")
    if not isinstance(entries, list) or not entries:
        raise SystemExit("ActionResult must contain a non-empty artifacts list")
    candidates: dict[str, pathlib.Path] = {}
    for path in artifact_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name in candidates:
            raise SystemExit(f"artifact directory contains duplicate basenames: {path.name}")
        candidates[path.name] = path

    seen = set()
    for expected in entries:
        if not isinstance(expected, dict):
            raise SystemExit("malformed artifact entry")
        name = safe_artifact_name(str(expected.get("name", "")))
        if name in seen:
            raise SystemExit(f"duplicate artifact name in result: {name}")
        seen.add(name)
        require_sha256(str(expected.get("digest", "")), "artifact digest")
        if not isinstance(expected.get("size"), int) or expected["size"] < 0:
            raise SystemExit("artifact size must be a non-negative integer")
        path = candidates.get(name)
        if path is None or artifact(path) != expected:
            raise SystemExit(f"artifact verification failed: {name}")


def result_path(key: str) -> str:
    return "actions/sha256/" + require_sha256(key, "action key").removeprefix("sha256:") + ".json"


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(required=True)
    key_parser = commands.add_parser("key")
    key_parser.add_argument("--recipe", required=True)
    key_parser.add_argument("--factory", default="manifests/package-factory.yaml")
    key_parser.add_argument("--root", default=".")
    key_parser.add_argument("--target", required=True)
    key_parser.add_argument("--arch", required=True)
    key_parser.add_argument("--image", required=True)
    key_parser.add_argument("--source-date-epoch", required=True, type=int)
    key_parser.add_argument("--dependency-key", action="append", default=[])
    native_parser = commands.add_parser("native-key")
    native_parser.add_argument("--identity", required=True)
    native_parser.add_argument("--manifest", required=True)
    native_parser.add_argument("--input", action="append", default=[])
    native_parser.add_argument("--dependency-tree", default="")
    native_parser.add_argument("--target-queue", default="")
    native_parser.add_argument("--track", default="stable")
    native_parser.add_argument("--series", default="")
    native_parser.add_argument("--factory", default="manifests/package-factory.yaml")
    native_parser.add_argument("--root", default=".")
    native_parser.add_argument("--target", required=True)
    native_parser.add_argument("--arch", required=True)
    native_parser.add_argument("--image", required=True)
    native_parser.add_argument("--source-date-epoch", required=True, type=int)
    result_parser = commands.add_parser("result")
    result_parser.add_argument("--action-key", required=True)
    result_parser.add_argument("--artifact", action="append", required=True)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--result", required=True)
    verify_parser.add_argument("--artifact-dir", required=True)
    verify_parser.add_argument("--expected-action-key")
    verify_parser.add_argument("--package")
    verify_parser.add_argument("--target")
    verify_parser.add_argument("--arch")
    verify_parser.add_argument("--quarantine", type=pathlib.Path)
    quar_parser = commands.add_parser("quarantine-check")
    quar_parser.add_argument("--package", required=True)
    quar_parser.add_argument("--target")
    quar_parser.add_argument("--arch")
    quar_parser.add_argument("--quarantine", type=pathlib.Path)
    path_parser = commands.add_parser("r2-path")
    path_parser.add_argument("--action-key", required=True)
    args = parser.parse_args()

    if args.__dict__.get("recipe"):
        inputs = action_inputs(args)
        print(json.dumps({"action_key": action_key(inputs), "inputs": inputs}, sort_keys=True))
    elif args.__dict__.get("identity"):
        inputs = native_action_inputs(args)
        print(json.dumps({"action_key": action_key(inputs), "inputs": inputs}, sort_keys=True))
    elif args.__dict__.get("artifact"):
        print(json.dumps(create_result(args.action_key, map(pathlib.Path, args.artifact)), sort_keys=True))
    elif args.__dict__.get("result"):
        result = json.loads(pathlib.Path(args.result).read_text(encoding="utf-8"))
        verify_result(
            result,
            pathlib.Path(args.artifact_dir),
            args.expected_action_key,
            quarantine_manifest=args.quarantine,
            package=args.package,
            target=args.target,
            arch=args.arch,
        )
        print("verified " + result["action_key"])
    elif args.__dict__.get("command") == "quarantine-check" or "package" in args.__dict__ and not args.__dict__.get("result"):
        quarantined = is_quarantined(args.package, args.target, args.arch, args.quarantine)
        print("quarantined" if quarantined else "not quarantined")
        return 0 if quarantined else 1
    else:
        print(result_path(args.action_key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
