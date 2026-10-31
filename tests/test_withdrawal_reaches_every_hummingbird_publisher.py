"""Withdrawal must reach hummingbird's served index however it is published.

Two independent paths can sync hummingbird/20251124-{x86_64,aarch64} to R2:
build-chain-fanout.yml's dedicated publish job (.github/scripts/
fanout-publish-one.sh), and publish-build-chain-rpms.yml's matrix publisher,
dispatchable with the hummingbird-x86_64 / hummingbird-aarch64 cells
declared in manifests/package-builds.yaml. A withdrawal wired into only one
of them is not a fix -- the next run through the OTHER path re-indexes the
withdrawn package straight back into repodata, silently (#525's own
pattern: "nothing withdraws the copy already published").

So both callers of scripts/publish-rpm-wave.sh must pass --exclude-file,
sourced from scripts/plan-withdrawn-excludes.py for the right target name.
"""
from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
FANOUT_SCRIPT = ROOT / ".github" / "scripts" / "fanout-publish-one.sh"
FANOUT_WORKFLOW = ROOT / ".github" / "workflows" / "build-chain-fanout.yml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish-build-chain-rpms.yml"
PLAN_SCRIPT = ROOT / "scripts" / "plan-withdrawn-excludes.py"
WAVE = ROOT / "scripts" / "publish-rpm-wave.sh"


def test_the_wave_publisher_accepts_an_exclude_file() -> None:
    text = WAVE.read_text(encoding="utf-8")
    assert "--exclude-file" in text
    assert "-x" in text, "an exclude file that never reaches createrepo_c -x does nothing"


def test_fanout_publish_one_plans_and_passes_excludes() -> None:
    text = FANOUT_SCRIPT.read_text(encoding="utf-8")
    assert "plan-withdrawn-excludes.py" in text, (
        "fanout-publish-one.sh must turn TARGET into an exclude file before "
        "calling publish-rpm-wave.sh, or a fanout publish re-serves every "
        "withdrawn package"
    )
    assert "--exclude-file" in text


def test_build_chain_fanout_sets_target_for_both_hummingbird_legs() -> None:
    """TARGET must be set on both the x86_64 and aarch64 publish steps --
    each is an independent fanout-publish-one.sh invocation with its own
    env block, so setting it on only one leaves the other leg un-withdrawn."""
    body = yaml.safe_load(FANOUT_WORKFLOW.read_text(encoding="utf-8"))
    steps = body["jobs"]["publish"]["steps"]
    publish_steps = [s for s in steps
                     if str(s.get("name", "")).startswith("Publish hummingbird-")]
    assert len(publish_steps) == 2, (
        "expected exactly the x86_64 and aarch64 publish steps; the "
        "withdrawal wiring below assumes both"
    )
    for step in publish_steps:
        assert step.get("env", {}).get("TARGET") == "hummingbird", (
            f"{step['name']} does not set TARGET=hummingbird, so "
            "fanout-publish-one.sh cannot plan its excludes"
        )


def test_publish_build_chain_rpms_plans_and_passes_excludes() -> None:
    """The ad-hoc/manual cells=hummingbird-x86_64 dispatch path, the second
    publisher that can reach the same prefix (manifests/package-builds.yaml
    registers hummingbird-x86_64 and hummingbird-aarch64 as native_builds
    cells), must honor withdrawal too."""
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    assert "plan-withdrawn-excludes.py" in text
    body = yaml.safe_load(text)
    steps = body["jobs"]["publish"]["steps"]
    sign_index = next(s for s in steps if s.get("name") == "Sign and index")
    assert "--exclude-file" in sign_index["run"]
    # The plan step must run with this job's own matrix.target, not a
    # literal "hummingbird" -- this publisher serves other families too,
    # and a hardcoded target would plan hummingbird's excludes while
    # actually publishing e.g. gnome50.
    plan_index = next(i for i, s in enumerate(steps)
                       if "plan-withdrawn-excludes.py" in s.get("run", ""))
    assert "matrix.target" in steps[plan_index]["run"]
    assert plan_index < steps.index(sign_index), (
        "the exclude file must be planned before Sign and index reads it"
    )


def test_hummingbird_cells_share_the_r2_path_fanout_publishes_to() -> None:
    """Pins the premise of this file: both paths really do write the same
    prefix, so both really do need the withdrawal wiring."""
    registry = yaml.safe_load(
        (ROOT / "manifests" / "package-builds.yaml").read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in registry["native_builds"]}
    assert by_id["hummingbird-x86_64"]["r2_path"] == "hummingbird/20251124-x86_64"
    assert by_id["hummingbird-x86_64"]["target"] == "hummingbird"
    fanout_text = FANOUT_WORKFLOW.read_text(encoding="utf-8")
    assert "hummingbird/20251124-x86_64" in fanout_text
