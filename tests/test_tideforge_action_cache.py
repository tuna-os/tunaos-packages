from __future__ import annotations

import argparse
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cache", ROOT / "scripts" / "tideforge-action-cache.py"
)
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


def fixture(tmp_path: pathlib.Path, **overrides) -> argparse.Namespace:
    recipe = tmp_path / "packages" / "demo" / "package.yaml"
    recipe.parent.mkdir(parents=True, exist_ok=True)
    recipe.write_text(
        "name: demo\ndependencies: {build: {capabilities: [compiler]}}\n",
        encoding="utf-8",
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    for name in (
        "tideforge.py",
        "assemble-deb-source-tree.py",
        "build-chain.sh",
        "run-package-factory-cell.sh",
        "fetch-tideforge-sources.py",
        "import-fedora-distgit.py",
    ):
        (scripts / name).write_text(f"# {name}\n", encoding="utf-8")
    factory = tmp_path / "factory.yaml"
    factory.write_text(
        "targets:\n"
        "  fedora: {format: rpm, architectures: [x86_64, aarch64]}\n"
        "  debian: {format: deb, architectures: [amd64]}\n"
        "dependency_catalog:\n"
        "  compiler: {fedora: [gcc], debian: [build-essential]}\n",
        encoding="utf-8",
    )
    values = {
        "root": str(tmp_path),
        "recipe": str(recipe),
        "factory": str(factory),
        "target": "fedora",
        "arch": "x86_64",
        "image": "registry.example/build@sha256:" + "a" * 64,
        "source_date_epoch": 1_700_000_000,
        "dependency_key": [],
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def key(args: argparse.Namespace) -> str:
    return cache.action_key(cache.action_inputs(args))


def test_key_is_stable_and_uses_repository_relative_recipe_path(tmp_path):
    first = cache.action_inputs(fixture(tmp_path))
    second = cache.action_inputs(fixture(tmp_path))
    assert cache.action_key(first) == cache.action_key(second)
    assert first["recipe"]["path"] == "packages/demo/package.yaml"


def test_key_changes_for_arch_dependency_and_epoch(tmp_path):
    base = key(fixture(tmp_path))
    assert base != key(fixture(tmp_path, arch="aarch64"))
    assert base != key(fixture(tmp_path, dependency_key=["sha256:" + "b" * 64]))
    assert base != key(fixture(tmp_path, source_date_epoch=1_700_000_001))


def test_target_slice_ignores_an_unrelated_target_change(tmp_path):
    args = fixture(tmp_path)
    before = key(args)
    factory = pathlib.Path(args.factory)
    factory.write_text(factory.read_text().replace("build-essential", "clang"), encoding="utf-8")
    assert key(args) == before


def test_target_slice_tracks_selected_dependency_capabilities(tmp_path):
    args = fixture(tmp_path)
    before = key(args)
    factory = pathlib.Path(args.factory)
    factory.write_text(factory.read_text().replace("[gcc]", "[gcc, gcc-c++]"), encoding="utf-8")
    assert key(args) != before


def test_target_slice_ignores_unconsumed_dependency_capabilities(tmp_path):
    args = fixture(tmp_path)
    before = key(args)
    factory = pathlib.Path(args.factory)
    factory.write_text(
        factory.read_text() + "  unused: {fedora: [unused-devel]}\n",
        encoding="utf-8",
    )
    assert key(args) == before


def test_renderer_inputs_are_partitioned_by_package_format(tmp_path):
    rpm = cache.action_inputs(fixture(tmp_path))
    deb = cache.action_inputs(fixture(tmp_path, target="debian", arch="amd64"))
    assert "scripts/build-chain.sh" not in rpm["renderer_inputs"]
    assert "scripts/assemble-deb-source-tree.py" not in rpm["renderer_inputs"]
    assert "scripts/assemble-deb-source-tree.py" in deb["renderer_inputs"]
    assert "scripts/build-chain.sh" not in deb["renderer_inputs"]
    assert "scripts/run-package-factory-cell.sh" in rpm["renderer_inputs"]
    assert "scripts/run-package-factory-cell.sh" in deb["renderer_inputs"]


def test_mutable_build_image_and_bad_dependency_key_are_refused(tmp_path):
    with pytest.raises(SystemExit, match="digest-pinned"):
        cache.action_inputs(fixture(tmp_path, image="registry.example/build:latest"))
    with pytest.raises(SystemExit, match="dependency action key"):
        cache.action_inputs(fixture(tmp_path, dependency_key=["not-a-digest"]))


def test_result_verification_detects_tampering_and_wrong_action(tmp_path):
    package = tmp_path / "x86_64" / "demo.rpm"
    package.parent.mkdir()
    package.write_bytes(b"first")
    action = "sha256:" + "c" * 64
    result = cache.create_result(action, [package])
    cache.verify_result(result, tmp_path, action)
    with pytest.raises(SystemExit, match="requested action"):
        cache.verify_result(result, tmp_path, "sha256:" + "d" * 64)
    package.write_bytes(b"second")
    with pytest.raises(SystemExit, match="verification failed"):
        cache.verify_result(result, tmp_path, action)


def test_result_rejects_path_traversal_empty_and_duplicate_artifacts(tmp_path):
    action = "sha256:" + "c" * 64
    with pytest.raises(SystemExit, match="at least one"):
        cache.create_result(action, [])
    bad = {
        "schema": 1,
        "action_key": action,
        "artifacts": [{"name": "../escape.rpm", "size": 0, "digest": "sha256:" + "0" * 64}],
    }
    with pytest.raises(SystemExit, match="unsafe artifact"):
        cache.verify_result(bad, tmp_path)
    package = tmp_path / "demo.rpm"
    package.write_bytes(b"ok")
    duplicate = cache.create_result(action, [package])
    duplicate["artifacts"].append(dict(duplicate["artifacts"][0]))
    with pytest.raises(SystemExit, match="duplicate"):
        cache.verify_result(duplicate, tmp_path)


def test_r2_path_is_canonical():
    key = "sha256:" + "f" * 64
    assert cache.result_path(key) == "actions/sha256/" + "f" * 64 + ".json"


def test_native_key_tracks_only_declared_source_trees(tmp_path):
    base = fixture(tmp_path)
    manifest = tmp_path / "order.yml"
    source = tmp_path / "src" / "gnome"
    source.mkdir(parents=True)
    manifest.write_text("tiers: {}\n")
    (source / "demo.spec").write_text("Version: 1\n")
    args = argparse.Namespace(
        root=base.root,
        factory=base.factory,
        identity="gnome-fedora",
        manifest=str(manifest),
        input=[str(source)],
        target="fedora",
        arch="x86_64",
        image=base.image,
        source_date_epoch=base.source_date_epoch,
        dependency_tree="",
        target_queue="",
        track="stable",
        series="50",
    )
    before = cache.action_key(cache.native_action_inputs(args))
    (source / "demo.spec").write_text("Version: 2\n")
    assert cache.action_key(cache.native_action_inputs(args)) != before


def test_native_distgit_importer_is_hashed_only_when_declared(tmp_path):
    base = fixture(tmp_path)
    manifest = tmp_path / "order.yml"
    source = tmp_path / "src"
    source.mkdir()
    args = argparse.Namespace(
        root=base.root, factory=base.factory, identity="native",
        manifest=str(manifest), input=[str(source)], target="fedora",
        arch="x86_64", image=base.image, source_date_epoch=base.source_date_epoch,
        dependency_tree="", target_queue="", track="stable", series="",
    )
    manifest.write_text("tiers: []\n")
    plain = cache.native_action_inputs(args)
    assert "scripts/import-fedora-distgit.py" not in plain["renderer_inputs"]
    manifest.write_text("tiers: [{packages: [{path: src/x, distgit: x}]}]\n")
    imported = cache.native_action_inputs(args)
    assert "scripts/import-fedora-distgit.py" in imported["renderer_inputs"]


def test_native_release_contract_hashes_only_selected_track_and_target(tmp_path):
    base = fixture(tmp_path)
    manifest = tmp_path / "order.yml"
    source = tmp_path / "src"
    source.mkdir()
    manifest.write_text("tiers: []\n")
    tree = tmp_path / "tree.yaml"
    tree.write_text(
        "schema: 1\nnodes: {demo: {needs: []}}\ntracks:\n"
        "  stable: {series: '50'}\n  next: {series: '51'}\n"
    )
    queue = tmp_path / "queue.yaml"
    queue.write_text("schema: 1\nqueues:\n  fedora: {gates: [install]}\n  debian: {gates: [install]}\n")
    args = argparse.Namespace(
        root=base.root, factory=base.factory, identity="gnome-stable",
        manifest=str(manifest), input=[str(source)], target="fedora",
        arch="x86_64", image=base.image, source_date_epoch=base.source_date_epoch,
        dependency_tree=str(tree), target_queue=str(queue), track="stable", series="50",
    )
    before = cache.action_key(cache.native_action_inputs(args))
    tree.write_text(tree.read_text().replace("series: '51'", "series: '52'"))
    assert cache.action_key(cache.native_action_inputs(args)) == before
    queue.write_text(queue.read_text().replace("debian: {gates: [install]}", "debian: {gates: [install, smoke]}"))
    assert cache.action_key(cache.native_action_inputs(args)) == before
    tree.write_text(tree.read_text().replace("series: '50'", "series: '50.1'"))
    assert cache.action_key(cache.native_action_inputs(args)) != before
