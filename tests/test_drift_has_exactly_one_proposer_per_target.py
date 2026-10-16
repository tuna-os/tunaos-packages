"""Ensure each gap_measurement.drift target has exactly one PR proposer.

When two workflows act on the same gap_measurement.drift block in propose mode
(tuna-os/tunaos-packages#688), competing pull requests are opened simultaneously
against the same base. Whichever PR merges second reverts the first, and the
loser's next scheduled run re-opens it.

This test holds the split:
  - upstream-drift.yml owns `mode: propose` (hourly, revision-gated, opens PRs).
  - gap-drift.yml owns `mode: exhibit` (dispatch-only, no schedule, no PRs).
  - For any target, exactly one workflow opens a PR for its drift block.
"""
from __future__ import annotations

import pathlib
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
MANIFEST = ROOT / "manifests" / "package-factory.yaml"


def load_manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def load_workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


def drift_targets_by_mode() -> dict[str, list[str]]:
    manifest = load_manifest()
    by_mode: dict[str, list[str]] = {}
    for name, spec in (manifest.get("targets") or {}).items():
        if not isinstance(spec, dict):
            continue
        drift = (spec.get("gap_measurement") or {}).get("drift")
        if isinstance(drift, dict) and "mode" in drift:
            by_mode.setdefault(drift["mode"], []).append(name)
    return by_mode


def test_gap_drift_workflow_has_no_schedule_or_pr_creation() -> None:
    """gap-drift.yml is dispatch-only and never creates pull requests."""
    wf = load_workflow("gap-drift.yml")

    # No schedule trigger -- exhibit mode is dispatch-only
    on = wf.get("on") or wf.get(True) or {}
    assert "schedule" not in on, (
        "gap-drift.yml must not have a schedule: trigger; propose targets "
        "are handled by upstream-drift.yml, and exhibit targets are dispatch-only"
    )
    assert "workflow_dispatch" in on, "gap-drift.yml must be dispatch-driven"

    # No create-pull-request step anywhere in gap-drift.yml
    raw_text = (WORKFLOWS_DIR / "gap-drift.yml").read_text(encoding="utf-8")
    assert "create-pull-request" not in raw_text, (
        "gap-drift.yml must not contain create-pull-request; exhibit mode "
        "only exhibits the diff and uploads artifacts without opening PRs"
    )
    assert "gh pr create" not in raw_text, (
        "gap-drift.yml must not create pull requests"
    )


def test_gap_drift_workflow_only_selects_exhibit_targets() -> None:
    """gap-drift.yml must only plan targets whose drift mode is exhibit."""
    raw_text = (WORKFLOWS_DIR / "gap-drift.yml").read_text(encoding="utf-8")
    assert 'drift.get("mode") != "exhibit"' in raw_text or "mode != 'exhibit'" in raw_text, (
        "gap-drift.yml plan step must filter out non-exhibit targets"
    )
    assert 'propose' not in raw_text.split("Plan exhibit targets")[1].split("EOF")[0], (
        "gap-drift.yml plan script must not select propose mode targets"
    )


def test_upstream_drift_workflow_owns_propose_targets() -> None:
    """upstream-drift.yml is scheduled and opens PRs with summarized diffs."""
    wf = load_workflow("upstream-drift.yml")
    raw_text = (WORKFLOWS_DIR / "upstream-drift.yml").read_text(encoding="utf-8")

    on = wf.get("on") or wf.get(True) or {}
    assert "schedule" in on, "upstream-drift.yml must have a schedule: trigger"
    assert "workflow_dispatch" in on, "upstream-drift.yml must support dispatch"

    assert "create-pull-request" in raw_text, (
        "upstream-drift.yml must open pull requests for drifted propose targets"
    )
    assert "summarize-gap-drift.py" in raw_text, (
        "upstream-drift.yml must summarize adds, drops, and moves for the PR body"
    )


def test_exactly_one_workflow_proposes_prs_per_drift_target() -> None:
    """For every target declaring gap_measurement.drift:
    - If mode == 'propose': exactly one workflow opens PRs (upstream-drift.yml).
    - If mode == 'exhibit': zero workflows open PRs.
    """
    by_mode = drift_targets_by_mode()
    propose_targets = set(by_mode.get("propose", []))
    exhibit_targets = set(by_mode.get("exhibit", []))

    assert "hummingbird" in propose_targets, "hummingbird must be in propose mode"
    assert "fedora" in exhibit_targets, "fedora must be in exhibit mode"

    # Find all workflows that create PRs for target drift
    drift_proposing_workflows = []
    for wf_path in WORKFLOWS_DIR.glob("*.yml"):
        content = wf_path.read_text(encoding="utf-8")
        if "measure-target-gap.py" in content and ("create-pull-request" in content or "gh pr create" in content):
            drift_proposing_workflows.append(wf_path.name)

    assert drift_proposing_workflows == ["upstream-drift.yml"], (
        f"Expected exactly ['upstream-drift.yml'] to propose gap drift PRs, "
        f"but found: {drift_proposing_workflows}"
    )
