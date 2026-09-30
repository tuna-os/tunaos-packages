"""Editing one target's manifest block must not re-key another target's cell.

A build-chain cell's action key decides whether the resume in
`restore-partial-chain-output.py` accepts the previous attempt's partial. If
the key moves, the partial is rejected -- "action key differs ... building
from scratch" -- and a 22-tier chain restarts at bootstrap-00.

`manifests/package-factory.yaml` is ONE file describing every target, and its
whole-file digest used to be in `native_inputs`. So an edit for any target
re-keyed every cell of every other target.

Measured, run 32842254545 (hummingbird-x86_64):

    11:27:19  [resume] found `hummingbird-x86_64-partial` from 11:26:17Z (429 MB)
    11:27:24  [resume] action key differs ... building from scratch
    11:40:54  ===== Tier: layer-00 =====        <- still here 3h48m later

#512 had edited the manifest to add `drift:` blocks and correct `probe_image`.
Neither changes what a single hummingbird RPM compiles to. Four hours of
runner time reached tier 5 of 22. See #528.

This also silently defeated #473, which had narrowed the manifest's
contribution to `build_view(target)` for exactly this reason -- and then the
whole-file digest reinstated every other target's fields.

The manifest reaches the key by TWO independent paths, and fixing one while
leaving the other looks exactly like a fix while changing nothing:

  * `native_inputs[].digest` -- the file's content;
  * `reproducibility.source_date_epoch` -- computed in package-factory-cell.yml
    as `git log -1 --format=%at -- <manifest> <source_paths>`, so any commit
    touching the manifest bumps it.

The first version of this file held the epoch constant and so proved only the
first half, while its name claimed the whole thing. Both are closed now, and
`test_the_epoch_is_not_derived_from_the_manifest` is what keeps the second one
closed.

The pair of tests below is the point. Isolation alone is easy to get by
dropping the manifest from the key entirely, which would be a cache that
cannot tell two different recipes apart -- so the second test requires the key
to STILL move when this target's own build-relevant fields change.
"""
from __future__ import annotations

import copy
import importlib.util
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifests" / "package-factory.yaml"


def cache_module():
    spec = importlib.util.spec_from_file_location(
        "tideforge_action_cache", ROOT / "scripts" / "tideforge-action-cache.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def real_manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def native_inputs_for(manifest: dict, target: str, tmp_path) -> dict:
    """Call native_action_inputs against a manifest written inside the repo.

    The renderer digests are read relative to --root, so root must be the real
    repository; the manifest therefore has to live inside it too. It is written
    to a uniquely-named scratch file and removed afterwards.
    """
    cache = cache_module()
    scratch = ROOT / f".manifest-key-probe-{tmp_path.name}.yaml"
    order = ROOT / f".build-order-probe-{tmp_path.name}.yml"
    scratch.write_text(yaml.safe_dump(manifest, sort_keys=True), encoding="utf-8")
    order.write_text("tiers: []\n", encoding="utf-8")

    class Args:
        pass

    args = Args()
    args.root = str(ROOT)
    args.factory = str(scratch)
    args.manifest = str(scratch)
    args.input = [str(order)]
    args.target = target
    args.arch = "x86_64"
    args.image = "example.invalid/builder@sha256:" + "c" * 64
    args.identity = f"{target}-x86_64"
    args.dependency_tree = ""
    args.target_queue = ""
    args.track = "stable"
    args.series = "1"
    args.source_date_epoch = "1787654862"
    try:
        return cache.native_action_inputs(args)
    finally:
        scratch.unlink(missing_ok=True)
        order.unlink(missing_ok=True)


def key_of(manifest: dict, target: str, tmp_path) -> str:
    cache = cache_module()
    return cache.action_key(native_inputs_for(manifest, target, tmp_path))


def a_native_target(manifest: dict) -> str:
    """Pick a real target this key path supports, so the fixture is not fiction."""
    for name, block in manifest["targets"].items():
        if "hummingbird" in name:
            return name
    return next(iter(manifest["targets"]))


def test_editing_another_targets_block_leaves_this_key_alone(tmp_path) -> None:
    manifest = real_manifest()
    target = a_native_target(manifest)
    others = [t for t in manifest["targets"] if t != target]
    assert others, "manifest has only one target; this test proves nothing"

    before = key_of(manifest, target, tmp_path)

    edited = copy.deepcopy(manifest)
    # The shape of #512's change: a field on a DIFFERENT target.
    victim = others[0]
    edited["targets"][victim]["probe_image"] = "example.invalid/probe@sha256:" + "d" * 64
    edited["targets"][victim].setdefault("gap_measurement", {})["drift"] = {"mode": "propose"}

    after = key_of(edited, target, tmp_path)
    assert before == after, (
        f"editing {victim}'s block moved {target}'s action key, so its partial "
        "is rejected and a 22-tier chain restarts at bootstrap-00 (#528)"
    )


def test_editing_this_targets_own_build_fields_still_moves_the_key(tmp_path) -> None:
    """The guard that stops the fix becoming a cache that cannot tell recipes apart."""
    manifest = real_manifest()
    target = a_native_target(manifest)
    before = key_of(manifest, target, tmp_path)

    edited = copy.deepcopy(manifest)
    edited["targets"][target]["build_repositories"] = [
        {"name": "invented", "baseurl": "https://example.invalid/repo", "priority": 5}
    ]

    after = key_of(edited, target, tmp_path)
    assert before != after, (
        "changing this target's own build repositories left the key unchanged; "
        "the cache can no longer tell two different recipes apart, which is "
        "far worse than the restart it was meant to prevent"
    )


def test_a_schema_bump_still_moves_the_key(tmp_path) -> None:
    """`schema` decides how every other field is read, so it must stay pinned."""
    manifest = real_manifest()
    target = a_native_target(manifest)
    before = key_of(manifest, target, tmp_path)

    edited = copy.deepcopy(manifest)
    edited["schema"] = int(manifest.get("schema", 1)) + 1

    after = key_of(edited, target, tmp_path)
    assert before != after, (
        "a manifest schema bump no longer invalidates the key, so cells would "
        "reuse output built under different field semantics"
    )


def test_the_epoch_is_not_derived_from_the_manifest() -> None:
    """The second path from the manifest into the key.

    The digest fix above is invisible without this one: the epoch is
    SOURCE_DATE_EPOCH *and* an action-key input, so while it was computed over
    the manifest, every edit re-keyed every cell no matter how well the digest
    was scoped. A test that only checked the digest would have passed on a
    change that fixed nothing in production.
    """
    workflow = (ROOT / ".github" / "workflows" / "package-factory-cell.yml").read_text(
        encoding="utf-8")
    line = next(
        line for line in workflow.splitlines()
        if "epoch=$(git log" in line and "source_paths" in line
    )
    assert "matrix.manifest" not in line, (
        "SOURCE_DATE_EPOCH is derived from the manifest again, so an edit to "
        "ANY target's block re-keys this cell and its partial is rejected "
        f"(#528). Line: {line.strip()}"
    )


def test_an_empty_source_path_list_fails_loudly() -> None:
    """`git log -1 --format=%at --` with no paths returns the repo's last commit.

    That would re-key every cell on every commit -- strictly worse than the
    bug being fixed -- and it would do so silently, because an epoch is just a
    number. Every build-chain cell declares source_paths today; the guard is
    for the one that some day does not.
    """
    workflow = (ROOT / ".github" / "workflows" / "package-factory-cell.yml").read_text(
        encoding="utf-8")
    epoch_at = workflow.index('epoch=$(git log -1 --format=%at -- "${source_paths[@]}")')
    preceding = workflow[:epoch_at]
    guard = preceding.rindex('if [ "${#source_paths[@]}" -eq 0 ]')
    assert "exit 1" in preceding[guard:], (
        "the empty-source_paths guard no longer exits, so a cell without "
        "sources would silently take the repository's last commit as its "
        "SOURCE_DATE_EPOCH and re-key on every push"
    )
