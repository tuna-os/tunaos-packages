"""Which build-chain cells may publish, and which must be refused.

The build-chain families declare an `r2_path` in the catalog, so the
destination is data. What the data does not settle is whether that
destination is SAFE, and two answers are no:

  collision   publish-tideforge-rpms.yml syncs into repo/10-stream-x86_64,
              repo/10-x86_64 and rpm/el10/aarch64. gnome50-el10-x86_64
              declared repo/10-x86_64 until 2026-09-03 (run 33751204743 was
              refused on it). `rclone sync` makes the destination
              match the source, so the two publishers would delete each
              other's packages -- serialising them stops a race, not an
              overwrite.
  no home     an empty r2_path, or publish: false, means the cell has
              nowhere to go. fprintd-el10-x86_64 is both.

These are pinned because the failure is silent and destructive: a wave that
looked green while emptying a live repo.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "plan-build-chain-publish.py"

_spec = importlib.util.spec_from_file_location("plan_bc", SCRIPT)
planner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(planner)


def cell(cid, r2_path="fam/10-x86_64", publish=None, **kw):
    c = {
        "id": cid, "target": "el10", "architecture": "x86_64",
        "runner": "ubuntu-24.04", "image": "img", "manifest": "m.yml",
        "mock_config": "mc", "r2_path": r2_path,
    }
    if publish is not None:
        c["publish"] = publish
    c.update(kw)
    return c


def avail(*cells):
    return {c["id"]: c for c in cells}


# --- the collision guard ----------------------------------------------------


@pytest.mark.parametrize("dest", sorted(planner.TIDEFORGE_DESTINATIONS))
def test_a_tideforge_destination_is_refused(dest) -> None:
    selected, rejections = planner.resolve(["x"], avail(cell("x", r2_path=dest)))
    assert selected == []
    assert len(rejections) == 1
    assert dest in rejections[0]
    assert "delete" in rejections[0]


def test_the_tideforge_mirror_prefix_stays_refused() -> None:
    """Pins the collision that actually happened rather than a hypothetical.

    publish-tideforge-rpms.yml mirrors x86_64 to repo/10-x86_64, and that is
    what gnome50-el10-x86_64 declared until it got its own prefix; the guard
    stays so the next family cannot make the same catalog mistake quietly.
    """
    assert "repo/10-x86_64" in planner.TIDEFORGE_DESTINATIONS


def test_qualified_real_build_chain_cells_keep_their_accepted_prefixes() -> None:
    """Every published family must own a prefix the planner accepts; a cell
    that trips the collision guard is a family that can never publish."""
    cells = planner.build_chain_cells()
    publishable = [c for c in cells.values() if c.get("publish") is not False and c.get("r2_path")
                   and c.get("target") not in {"alma10", "alma10-kitten"}]
    selected, rejections = planner.resolve([c["id"] for c in publishable], cells)
    assert rejections == [], rejections
    assert {c["id"] for c in selected} == {c["id"] for c in publishable}


def test_a_leading_or_trailing_slash_does_not_evade_the_guard() -> None:
    selected, rejections = planner.resolve(
        ["x"], avail(cell("x", r2_path="/repo/10-x86_64/"))
    )
    assert selected == []
    assert "delete" in rejections[0]


# --- cells with nowhere to go -----------------------------------------------


def test_publish_false_is_refused() -> None:
    selected, rejections = planner.resolve(["x"], avail(cell("x", publish=False)))
    assert selected == []
    assert "publish: false" in rejections[0]


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_a_missing_r2_path_is_refused(empty) -> None:
    selected, rejections = planner.resolve(["x"], avail(cell("x", r2_path=empty)))
    assert selected == []
    assert "no r2_path" in rejections[0]


def test_an_unknown_cell_is_refused_and_lists_what_exists() -> None:
    selected, rejections = planner.resolve(["nope"], avail(cell("real")))
    assert selected == []
    assert "not a build-chain cell" in rejections[0]
    assert "real" in rejections[0]


# --- the happy path ---------------------------------------------------------


def test_a_safe_cell_is_selected_with_its_destination() -> None:
    selected, rejections = planner.resolve(
        ["x"], avail(cell("x", r2_path="xfce/10-stream-x86_64"))
    )
    assert rejections == []
    assert selected[0]["id"] == "x"
    assert selected[0]["r2_path"] == "xfce/10-stream-x86_64"


def test_publish_unset_is_allowed() -> None:
    """Most build-chain cells leave `publish` unset; only false opts out."""
    selected, _ = planner.resolve(["x"], avail(cell("x")))
    assert len(selected) == 1


# --- all-or-nothing ---------------------------------------------------------


def test_one_bad_cell_fails_the_whole_wave(tmp_path) -> None:
    """A partial publish that silently dropped a requested cell is worse than
    none: the wave reads green and the package is still missing."""
    available = avail(cell("good"), cell("bad", r2_path="repo/10-x86_64"))
    rc = planner.main(["--cells", "good,bad", "--github-output", str(tmp_path / "o")])
    # main() re-reads the live catalog, so assert on resolve() for the unit
    # and only that main() is wired to fail on any rejection.
    selected, rejections = planner.resolve(["good", "bad"], available)
    assert len(selected) == 1 and len(rejections) == 1
    assert rc != 0


def test_every_rejection_is_reported_not_just_the_first() -> None:
    available = avail(
        cell("a", r2_path="repo/10-x86_64"),
        cell("b", publish=False),
        cell("c", r2_path=""),
    )
    _, rejections = planner.resolve(["a", "b", "c"], available)
    assert len(rejections) == 3


@pytest.mark.parametrize("target", ["alma10", "alma10-kitten"])
def test_candidate_cells_cannot_opt_in_to_production(target):
    available = avail(cell("candidate", target=target, publish=True,
                           r2_path=f"gnome51/{target}-x86_64"))
    selected, rejections = planner.resolve(["candidate"], available)
    assert selected == []
    assert len(rejections) == 1
    assert "candidate-only" in rejections[0]


def test_all_real_alma_cells_are_refused_before_any_matrix_is_written(tmp_path):
    cells = planner.build_chain_cells()
    candidates = [c["id"] for c in cells.values()
                  if c.get("target") in {"alma10", "alma10-kitten"}]
    assert candidates
    selected, rejections = planner.resolve(candidates, cells)
    assert not selected
    assert len(rejections) == len(candidates)
    assert all("candidate-only" in rejection for rejection in rejections)
    output = tmp_path / "github-output"
    assert planner.main(["--cells", ",".join(candidates),
                         "--github-output", str(output)]) != 0
    assert not output.exists()


def test_mixed_candidate_and_qualified_wave_fails_without_outputs(tmp_path):
    cells = planner.build_chain_cells()
    candidate = next(c["id"] for c in cells.values() if c.get("target") == "alma10")
    qualified = next(c["id"] for c in cells.values()
                     if c.get("target") == "el10" and c.get("r2_path")
                     and c.get("publish") is not False
                     and c["r2_path"] not in planner.TIDEFORGE_DESTINATIONS)
    output = tmp_path / "github-output"
    assert planner.main(["--cells", f"{qualified},{candidate}",
                         "--github-output", str(output)]) != 0
    assert not output.exists()


def test_production_secret_job_requires_successful_plan():
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish-build-chain-rpms.yml").read_text())
    job = workflow["jobs"]["publish"]
    assert "plan" in job["needs"]
    # This publisher intentionally runs after a failed build; its explicit
    # plan-success guard must still prevent credentials after candidate refusal.
    assert "needs.plan.result == 'success'" in job["if"]
    plan = workflow["jobs"]["plan"]
    assert "secrets.GPG_PRIVATE_KEY" not in str(plan)
    assert "secrets.R2_ACCESS_KEY_ID" not in str(plan)
    assert "scripts/plan-build-chain-publish.py" in str(plan)


@pytest.mark.parametrize("target", [None, "", "el10", "alma-alias"])
@pytest.mark.parametrize("destination", ["rpm/alma10/x86_64", "rpm/alma10-kitten/aarch64",
                                         "/gnome51/alma10-x86_64/", "xfce/alma10-kitten-aarch64"])
def test_alma_destination_cannot_bypass_candidate_fence(target, destination):
    candidate = cell("candidate", target=target, r2_path=destination)
    if target is None:
        del candidate["target"]
    selected, rejections = planner.resolve(["candidate"], avail(candidate))
    assert not selected
    assert len(rejections) == 1
    assert "candidate-only" in rejections[0]
