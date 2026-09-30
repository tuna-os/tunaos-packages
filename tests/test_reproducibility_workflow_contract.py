"""Unit tests for the reproducibility verifier workflow contract."""

from __future__ import annotations

import pathlib
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "reproducibility-verifier.yml"


def test_reproducibility_workflow_structure():
    assert WORKFLOW_PATH.is_file(), "reproducibility-verifier.yml must exist"
    doc = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    # Event triggers
    on_events = doc.get("on") or doc.get(True) or {}
    assert "schedule" in on_events
    assert "workflow_dispatch" in on_events
    assert "pull_request" in on_events

    # Concurrency
    concurrency = doc.get("concurrency") or {}
    assert concurrency.get("group") == "reproducibility-verifier"
    assert concurrency.get("cancel-in-progress") is False

    # Jobs
    jobs = doc.get("jobs") or {}
    assert "plan" in jobs
    assert "rebuild" in jobs
    assert "report" in jobs

    assert jobs["rebuild"].get("needs") == "plan"
    report_needs = jobs["report"].get("needs")
    assert report_needs in (["plan", "rebuild"], ["rebuild", "plan"])


def test_reproducibility_workflow_commands():
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "scripts/verify-reproducibility.py sample" in text
    assert "scripts/verify-reproducibility.py compare-dirs" in text
    assert "scripts/verify-reproducibility.py collect" in text
    assert "scripts/factory-status.py" in text
