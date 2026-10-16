"""A build that banks zero RPMs must fail in the build job, not the publish job.

Run 38901123271 dispatched tiers=local-00, which queued only tuna-desktop;
the published index already served that NVR, so the chain exited 0 having
banked 0 RPMs. The build job still went green: oras staged 0 RPMs,
upload-artifact (if-no-files-found: warn) uploaded nothing, and the publish
job died at download-artifact with "Artifact not found for name:
publish-rpm-hummingbird-x86_64" -- far from the cause. The build job must
refuse the empty wave itself, where the cell id, the tiers slice and the
served state are all visible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / ".github" / "workflows" / "publish-build-chain-rpms.yml"


def build_steps() -> list[dict]:
    return yaml.safe_load(PUBLISHER.read_text(encoding="utf-8"))["jobs"]["build"]["steps"]


def refusal_step() -> dict:
    for step in build_steps():
        if step.get("name") == "Refuse an empty wave":
            return step
    raise AssertionError("the build job has no 'Refuse an empty wave' step")


def success_upload() -> dict:
    for step in build_steps():
        with_ = step.get("with") or {}
        if "upload-artifact" in str(step.get("uses", "")) and str(with_.get("name", "")).startswith(
            "publish-rpm-"
        ):
            return step
    raise AssertionError("the build job has no publish-rpm-* upload step")


def test_the_build_job_refuses_an_empty_wave():
    assert refusal_step()


def test_the_refusal_runs_after_the_build_and_before_the_upload():
    names = [s.get("name") or s.get("uses", "") for s in build_steps()]
    build_at = next(i for i, n in enumerate(names) if n == "Build the cell")
    refuse_at = next(i for i, n in enumerate(names) if n == "Refuse an empty wave")
    upload_at = next(
        i
        for i, s in enumerate(build_steps())
        if "upload-artifact" in str(s.get("uses", ""))
        and str((s.get("with") or {}).get("name", "")).startswith("publish-rpm-")
    )
    assert build_at < refuse_at < upload_at


def test_the_refusal_counts_rpms_and_fails_the_job_when_none_were_banked():
    body = refusal_step()["run"]
    assert "*.rpm" in body
    assert "exit 1" in body


def test_the_refusal_names_the_cause_it_saw_in_run_38901123271():
    """The message must point at the already-served/tiers-slice shape, and at
    the download-artifact error the publish job would otherwise die with."""
    body = refusal_step()["run"]
    assert "0 RPM" in body
    assert "tiers" in body
    assert "Artifact not found" in body


def test_the_refusal_cannot_mask_the_builds_own_failure():
    """On a failed or cancelled build the step must not run: the build's own
    error is the one that matters. A cache hit is admitted because the
    restored output was already verified."""
    condition = refusal_step().get("if", "")
    assert "steps.verdict.outputs.hit == 'true'" in condition
    assert "steps.build.outcome == 'success'" in condition
    assert "failure()" not in condition
    assert "!= 'success'" not in condition


def test_the_success_upload_stays_warn_because_the_refusal_owns_the_failure():
    """warn, not error: with the refusal step in place, a leg that failed or
    banked nothing must report its own error, not an upload error."""
    assert success_upload()["with"]["if-no-files-found"] == "warn"
