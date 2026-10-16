"""A shared-code PR builds a rotating slice of its canaries, not all of them.

scripts/tideforge.py, the cell workflow or build-chain.sh cannot be attributed
to one package, so the planner selects one canary per family or target: 12-17
cells on every PR push, each covering the same diff, on a 60-job org queue.

On pull_request the workflow passes --round-robin-size K and the run number:
K of those shared-input cells build, rotated per push, while a cell whose OWN
recipe, manifest or sources changed always builds. The push to main and the
schedules never pass it, so the whole set still builds on merge.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
PLANNER = ROOT / "scripts" / "plan-package-factory.py"
WORKFLOW = ROOT / ".github" / "workflows" / "package-factory.yml"


@pytest.fixture(scope="module")
def planner():
    spec = importlib.util.spec_from_file_location("plan_package_factory", PLANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cells(planner):
    return planner.all_cells(ROOT)


def plan(planner, cells, changed: list[str], size: int = 0, seed: int = 0) -> list[str]:
    selected = planner.select_cells(cells, changed, canary_common=True)
    kept, _ = planner.round_robin(selected, changed, size=size, seed=seed)
    return [cell["id"] for cell in kept]


@pytest.mark.parametrize("shared", ["scripts/tideforge.py", "scripts/build-chain.sh"])
def test_a_shared_change_builds_k_cells_and_rotation_covers_the_set(planner, cells, shared):
    full = set(plan(planner, cells, [shared]))
    assert len(full) > 3
    seen: set[str] = set()
    for seed in range(len(full)):
        picked = plan(planner, cells, [shared], size=3, seed=seed)
        assert len(picked) == 3
        assert set(picked) <= full
        seen.update(picked)
    assert seen == full, f"rotation never reaches {sorted(full - seen)}"


def test_consecutive_runs_pick_different_cells(planner, cells):
    changed = ["scripts/build-chain.sh"]
    assert plan(planner, cells, changed, 3, 0) != plan(planner, cells, changed, 3, 1)


def test_cells_whose_own_inputs_changed_always_build(planner, cells):
    recipe = "packages/greetd/package.yaml"
    own = set(plan(planner, cells, [recipe]))
    assert own and all("greetd" in cell for cell in own)
    for seed in range(4):
        picked = set(plan(planner, cells, [recipe, "scripts/build-chain.sh"], size=3, seed=seed))
        assert own <= picked
        assert len(picked - own) == 3


def test_a_recipe_only_pr_is_not_rotated(planner, cells):
    recipe = ["packages/greetd/package.yaml"]
    assert plan(planner, cells, recipe, size=1, seed=7) == plan(planner, cells, recipe)


def test_size_zero_builds_the_whole_set(planner, cells):
    changed = ["scripts/tideforge.py"]
    assert plan(planner, cells, changed) == plan(planner, cells, changed, size=0, seed=5)


def test_the_cli_applies_the_rotation():
    tmp = ROOT / ".pytest-rr-changed.txt"
    tmp.write_text("scripts/build-chain.sh\n", encoding="utf-8")
    try:
        out = subprocess.run(
            [sys.executable, str(PLANNER), "--canary-common", "--changed-files", str(tmp),
             "--round-robin-size", "2", "--round-robin-seed", "9"],
            capture_output=True, text=True, check=True, cwd=ROOT,
        )
    finally:
        tmp.unlink(missing_ok=True)
    assert json.loads(out.stdout)["count"] == 2
    assert "deferring" in out.stderr


def test_only_pull_requests_rotate():
    """The push to main is what builds the cells a PR deferred."""
    run = WORKFLOW.read_text(encoding="utf-8")
    guard_at = run.index("--round-robin-size")
    guard = run[run.rindex("if [", 0, guard_at):guard_at]
    assert '"$EVENT_NAME" = pull_request' in guard
