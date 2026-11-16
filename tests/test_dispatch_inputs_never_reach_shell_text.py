"""Dispatch inputs reach the tideforge planners as values, never as script (#659).

GitHub substitutes `${{ inputs.x }}` into a `run:` script before bash parses
it, so single quotes are no defence: an input containing `'` ends the quote and
the rest runs as shell. The plan job's output becomes the matrix of the publish
job, which holds the signing key and the R2 credentials. Inputs therefore go
through step `env:` and are read as quoted shell variables.
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
PUBLISHERS = [
    "publish-tideforge-debs.yml",
    "publish-tideforge-rpms.yml",
    "publish-tideforge-arch.yml",
]
INPUT_EXPR = re.compile(r"\$\{\{[^}]*\binputs\.")


@pytest.mark.parametrize("name", PUBLISHERS)
def test_no_run_script_interpolates_a_dispatch_input(name):
    jobs = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["jobs"]
    offenders = [
        (job_name, step.get("name") or step.get("id"))
        for job_name, job in jobs.items()
        for step in job.get("steps", [])
        if INPUT_EXPR.search(step.get("run", ""))
    ]
    assert offenders == [], f"{name}: inputs expanded into shell text in {offenders}"


@pytest.mark.parametrize("name", PUBLISHERS)
def test_the_planner_still_receives_every_input(name):
    plan = next(s for s in yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
                ["jobs"]["plan"]["steps"] if s.get("id") == "plan")
    for var, value in plan["env"].items():
        assert INPUT_EXPR.search(value), (var, value)
        assert f'"${var}"' in plan["run"], f"{name}: {var} is set but never passed"
