"""A served deb wave can be re-verified without being rebuilt or republished.

Run 43632396278 published tuna-desktop 1.0.0~beta.3-1 and only its verify
jobs failed (a harness fault). Re-dispatching the full workflow would rebuild
on a cache miss and sync new bytes under the already-served version, so a
verify_run_id dispatch skips build and publish and verifies against the
provenance that earlier run retained.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "publish-tideforge-debs.yml"


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_verify_only_skips_build_and_publish():
    jobs = workflow()["jobs"]
    for name in ("build", "publish"):
        assert jobs[name]["if"] == "inputs.verify_run_id == ''", name


def test_verify_runs_after_a_publish_or_for_an_earlier_run():
    condition = " ".join(workflow()["jobs"]["verify"]["if"].split())
    assert "!cancelled()" in condition
    assert "needs.plan.result == 'success'" in condition
    assert "inputs.verify_run_id != '' || needs.publish.result == 'success'" in condition


def test_verify_reads_the_earlier_runs_provenance():
    verify = workflow()["jobs"]["verify"]
    assert verify["permissions"]["actions"] == "read"
    download = next(
        step for step in verify["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    )
    assert download["with"]["run-id"] == "${{ inputs.verify_run_id || github.run_id }}"
    assert download["with"]["github-token"] == (
        "${{ inputs.verify_run_id != '' && github.token || '' }}"
    )


def test_the_run_id_is_validated_before_anything_runs():
    plan = workflow()["jobs"]["plan"]
    step = next(step for step in plan["steps"] if step.get("id") == "plan")
    assert step["env"]["VERIFY_RUN_ID"] == "${{ inputs.verify_run_id }}"
    assert '"$VERIFY_RUN_ID" =~ ^[0-9]+$' in step["run"]
