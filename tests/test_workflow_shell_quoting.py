"""Single-quoted inline container scripts may not contain apostrophes."""
from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
OPENER = "bash -lc '"


def test_single_quoted_docker_scripts_contain_no_apostrophe() -> None:
    checked = 0
    for workflow_path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                run = step.get("run") or ""
                start = 0
                while (index := run.find(OPENER, start)) != -1:
                    rest = run[index + len(OPENER):]
                    end = rest.rfind("'")
                    assert end != -1, (workflow_path.name, job_name, "unterminated")
                    inner = rest[:end]
                    assert "'" not in inner, (workflow_path.name, job_name)
                    checked += 1
                    start = index + len(OPENER) + end + 1
    assert checked >= 1
