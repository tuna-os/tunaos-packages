"""A renderer-script change must not rebuild every chain end to end.

`scripts/build-chain.sh` and `scripts/parse-build-order.py` are renderer
inputs: an edit to them cannot be attributed to any package, so every
build-chain family gets selected. Until now each of those cells then built its
family's WHOLE chain on the PR gate.

Measured on #576's gate -- a two-line build-chain.sh fix:

    cell                    duration
    xfce-el10-x86_64        51m
    xfce-el10-aarch64       38m
    gnome50/51 el10 x4      >50m, unfinished
    cells selected          36

None of those packages can compile differently because of that diff. The gate
was buying nothing with the hours.

The fix is depth, not breadth. Every family still runs -- only its FIRST tier
does. `hummingbird` already had `canary_tiers: bootstrap-00`; the other
families were simply never given one.

What must stay true, and is asserted below:

  * the nightly, publish legs and fan-out never pass canary_common, so they
    still build everything;
  * an ordinary cell's OWN manifest or sources are never bounded -- a spec
    change must build the real thing; build-tool-only sources are bounded to
    their own first tier;
  * every family survives the bounding (see the canary_cells collapse hazard
    in test_every_family_survives_the_bounding);
  * every declared canary tier NAMES A REAL TIER -- a typo would build zero
    packages and pass vacuously, which is worse than the slow gate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
PLANNER = ROOT / "scripts" / "plan-package-factory.py"
PARSER = ROOT / "scripts" / "parse-build-order.py"
BUILDS = ROOT / "manifests" / "package-builds.yaml"


def plan(changed: list[str] | None, canary: bool) -> list[dict]:
    cmd = [sys.executable, str(PLANNER)]
    if canary:
        cmd.append("--canary-common")
    if changed is not None:
        tmp = ROOT / ".pytest-changed.txt"
        tmp.write_text("\n".join(changed) + "\n", encoding="utf-8")
        cmd += ["--changed-files", str(tmp)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=ROOT).stdout
    finally:
        if changed is not None:
            (ROOT / ".pytest-changed.txt").unlink(missing_ok=True)
    data = json.loads(out)
    return [c for m in data["matrices"] for c in json.loads(m)["include"]]


def native_cells() -> list[dict]:
    data = yaml.safe_load(BUILDS.read_text(encoding="utf-8"))

    def walk(node):
        if isinstance(node, dict):
            if "manifest" in node and "mock_config" in node:
                yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    return list(walk(data))


# --- the manifest ------------------------------------------------------------

def test_every_build_chain_family_declares_a_canary_tier():
    """A family without one falls back to building its entire chain, which is
    the slow gate this exists to remove -- silently, for the new family only."""
    missing = [c["id"] for c in native_cells() if not c.get("canary_tiers")]
    assert not missing, f"unbounded on the PR gate: {missing}"


@pytest.mark.parametrize("cell", native_cells(), ids=lambda c: c["id"])
def test_the_declared_canary_tier_exists_in_that_manifest(cell):
    """A typo builds ZERO packages and passes, which is worse than slow.

    This is the assertion that makes the rest of the change safe to trust.
    """
    manifest = ROOT / cell["manifest"]
    if not manifest.is_file():
        pytest.skip(f"{cell['manifest']} not present")
    tiers = subprocess.run(
        [sys.executable, str(PARSER), str(manifest), "--tiers"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert cell["canary_tiers"] in tiers, (
        f"{cell['id']}: canary_tiers={cell['canary_tiers']!r} is not a tier of "
        f"{cell['manifest']} (has {tiers[:6]}...)"
    )


# --- the planner -------------------------------------------------------------

def test_a_renderer_change_is_bounded_on_a_pull_request():
    cells = plan(["scripts/build-chain.sh"], canary=True)
    assert cells, "a renderer change must still gate something"
    unbounded = [c["id"] for c in cells if not c.get("tiers")]
    assert not unbounded, f"still building whole chains on a PR: {unbounded}"


def test_the_bounding_drops_the_continuation_shards():
    """-c1/-c2 exist to resume a long chain; one tier has nothing to resume."""
    cells = plan(["scripts/build-chain.sh"], canary=True)
    conts = [c["id"] for c in cells if c["id"].endswith(("-c1", "-c2"))]
    assert not conts, f"continuations on a bounded gate: {conts}"


def test_every_family_survives_the_bounding():
    """NOT canary_cells(): that collapses to one cell per (engine, target,
    format, architecture), and gnome50/gnome51/xfce/fprintd all share the el10
    rpm coordinate -- they would become a single representative and a renderer
    bug specific to the gnome51 chain would sail through."""
    families = {c["id"].split("-el10")[0].split("-x86_64")[0].split("-aarch64")[0]
                for c in plan(["scripts/build-chain.sh"], canary=True)}
    for expected in ("gnome50", "gnome51", "xfce", "hummingbird", "fprintd"):
        assert any(expected in f for f in families), (
            f"{expected} lost its coverage: {sorted(families)}"
        )


def test_a_source_change_still_builds_the_real_chain():
    """The cell that owns the change must never be bounded, even though the
    same PR could also trip a renderer rule."""
    cells = plan(["src/gnome-51/gtk4/gtk4.spec"], canary=True)
    assert cells, "a spec change must gate something"
    for cell in cells:
        assert not cell.get("tiers"), (
            f"{cell['id']} was bounded, so the spec change was never really built"
        )


def test_a_build_tool_source_change_is_bounded_to_its_first_tier():
    cells = plan(["src/deps/yq/yq.spec"], canary=True)
    assert cells, "a build-tool change must gate its owning families"
    assert {cell["family"] for cell in cells} == {"gnome50", "gnome51"}
    assert all(cell["id"].endswith("-canary") for cell in cells)
    assert {cell["tiers"] for cell in cells} == {"base-tools"}

def test_a_source_change_beside_a_renderer_change_still_builds_for_real():
    """Ordering matters: the owns-the-change check runs BEFORE the renderer
    rules, so a mixed PR does not quietly downgrade to a canary."""
    cells = plan(["scripts/build-chain.sh", "src/gnome-51/gtk4/gtk4.spec"], canary=True)
    owners = [c for c in cells if c["id"].startswith("gnome51-el10")]
    assert owners, "the owning family must be selected"
    for cell in owners:
        assert not cell.get("tiers"), f"{cell['id']} downgraded to a canary"


def test_the_nightly_is_never_bounded():
    """canary_common is passed only for pull_request/merge_group/push."""
    cells = [c for c in plan(None, canary=False) if c["engine"] == "build-chain"]
    assert cells
    bounded = [c["id"] for c in cells if c.get("tiers")]
    assert not bounded, f"the nightly must build everything: {bounded}"
