"""A continuation shard must compute the SAME action key as the leg it resumes.

The chain could not accumulate, and this is why. A cell that runs out of
clock banks `<base_id>-partial`; the next leg restores it and continues.
Restore only accepts a partial whose recorded action key matches the key the
resuming cell computes -- correctly, since a partial built from different
inputs is not resumable.

But `identity` is one of the hashed key inputs, and the cell workflow passed
`matrix.id`, which for a continuation carries a `-c1` suffix. So leg 2 asked
for a key that leg 1's partial could not possibly carry. Observed on
gnome51-el10-aarch64: all five candidates rejected as "action key differs",
including a 378 MB partial banked by the SAME RUN's leg 0 nineteen minutes
earlier, off the same commit. Then "building from scratch", at bootstrap-00.

Every other place already used base_id -- the partial is named by it and
looked up by it. Only the key computation was left on `matrix.id`.

These tests read BOTH files that call `native-key`, because fixing one and
leaving the other is the exact shape of the #529 epoch bug.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CELL = ROOT / ".github/workflows/package-factory-cell.yml"
PUBLISHER = ROOT / ".github/workflows/publish-build-chain-rpms.yml"
PLANNER = ROOT / "scripts/plan-build-chain-publish.py"

IDENTITY = re.compile(r"--identity\s+'([^']+)'")


def identity_arguments(path: Path) -> list[str]:
    return IDENTITY.findall(path.read_text(encoding="utf-8"))


def test_every_native_key_call_site_was_examined():
    """Guard against a check that is named for two files and reads one."""
    assert identity_arguments(CELL), f"no --identity found in {CELL.name}"
    assert identity_arguments(PUBLISHER), f"no --identity found in {PUBLISHER.name}"


def test_the_cell_workflow_keys_a_continuation_by_its_base():
    for argument in identity_arguments(CELL):
        assert "base_id" in argument, (
            f"package-factory-cell.yml computes its action key from {argument!r}. "
            "A continuation shard's id carries a -cN suffix, so it would hash to "
            "a key no banked partial can carry and every resume restarts the "
            "chain at bootstrap-00. Use matrix.base_id || matrix.id."
        )


def test_the_publisher_may_use_id_only_because_it_has_no_continuations():
    """`matrix.id` is correct there ONLY while the planner emits no base_id.

    The publisher resumes the nightly's partial, so its identity has to equal
    the nightly's BASE identity. That holds because its cell id IS the base.
    If the planner ever starts emitting base_id, this file needs the same
    treatment as the cell workflow, and this test is what will say so.
    """
    assert "base_id" not in PLANNER.read_text(encoding="utf-8"), (
        "the publish planner now emits base_id, so publish-build-chain-rpms.yml "
        "must key by base_id || id like package-factory-cell.yml does"
    )


@pytest.mark.parametrize("identity, expected", [
    ("gnome51-el10-aarch64", "gnome51-el10-aarch64"),
    ("gnome51-el10-aarch64-c1", "gnome51-el10-aarch64-c1"),
])
def test_identity_actually_changes_the_action_key(identity, expected, tmp_path):
    """The premise: identity is hashed, so a -c1 suffix moves the key.

    If this ever stops holding the fix above is pointless, and a test that
    cannot fail would be hiding that.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tideforge_action_cache", ROOT / "scripts/tideforge-action-cache.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = {"schema": 1, "identity": "gnome51-el10-aarch64"}
    other = {"schema": 1, "identity": identity}
    assert other["identity"] == expected
    same = module.action_key(base) == module.action_key(other)
    assert same == (identity == "gnome51-el10-aarch64"), (
        "identity must be part of the hashed key for the -cN suffix to have "
        "been the bug; if it is not, re-diagnose before trusting the fix"
    )


def test_a_canary_never_inherits_the_chains_identity():
    """The hazard this fix could have introduced, ruled out.

    Keying by `base_id || id` would be dangerous if a canary cell carried
    base_id: a canary truncated to bootstrap-00 would compute the full
    chain's action key, and could restore -- or be mistaken for -- the
    converging chain's partial. It does not. `canary_cells()` only appends
    `-canary` to the id and swaps in canary_tiers; `base_id` is set in one
    place, the continuation path, so a canary falls back to its own id and
    keeps banking to `<id>-canary-partial`.
    """
    planner = (ROOT / "scripts/plan-package-factory.py").read_text(encoding="utf-8")
    start = planner.index("def canary_cells(")
    end = planner.index("\ndef ", start + 1)
    assert "base_id" not in planner[start:end], (
        "canary_cells() now touches base_id; with the workflow keying by "
        "base_id || id a canary could compute the full chain's action key "
        "and collide with the converging chain's partial"
    )
    assert planner.count('["base_id"] =') == 1, (
        "base_id is assigned somewhere new; confirm it is still only the "
        "continuation path before trusting the identity fallback"
    )
