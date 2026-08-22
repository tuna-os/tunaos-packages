"""Every build-chain family must be built on a schedule, not only on demand.

The nightly hard-codes `family=hummingbird-desktops` and overrides any
selector, and it was the only unattended run this workflow had. So xfce (el10
and fedora, both arches), gnome50, gnome51 and fprintd were built only when
somebody happened to touch their sources.

That is the gap between "the factory builds our desktops" and "the factory
built our desktops the last time anyone changed them". A CentOS Stream rebase
can break a desktop family for weeks with every check on main still green,
because nothing asks -- and #480's whole finding, that
xfce/10-stream-aarch64 404s while x86_64 serves 110 names, is the same shape:
absence of a run reads identically to success.

A second cron carrying `engine=build-chain` closes it. The selector comes from
WHICH cron fired -- a scheduled event has no inputs, but `github.event.schedule`
is the cron expression verbatim.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "package-factory.yml"
WEEKLY = "0 3 * * 0"
NIGHTLY = "0 12 * * *"


def spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def triggers() -> dict:
    parsed = spec()
    return parsed[True] if True in parsed else parsed["on"]


def plan_step() -> dict:
    return next(
        step
        for step in spec()["jobs"]["plan"]["steps"]
        if step.get("id") == "plan"
    )


def planned(*args: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "plan-package-factory.py"), *args],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    return [cell for m in payload["matrices"] for cell in json.loads(m)["include"]]


def test_there_are_two_schedules():
    crons = [entry["cron"] for entry in triggers()["schedule"]]
    assert NIGHTLY in crons
    assert WEEKLY in crons


def test_the_weekly_selects_every_build_chain_family():
    run = plan_step()["run"]
    assert f"'{WEEKLY}') REQUESTED_SELECTOR=engine=build-chain" in run


def test_the_nightly_still_selects_hummingbird():
    """The weekly is an addition, not a replacement: hummingbird is the
    flagship and its drift against upstream is tracked nightly."""
    run = plan_step()["run"]
    assert "REQUESTED_SELECTOR=family=hummingbird-desktops" in run


def test_the_cron_reaches_the_step():
    """github.event.schedule is the only thing distinguishing the two, and a
    step cannot read it unless it is passed in."""
    assert plan_step()["env"]["SCHEDULE"] == "${{ github.event.schedule || '' }}"


def scheduled_selector(cron: str) -> str:
    """The selector the workflow itself would resolve for this cron.

    Read out of the case arm rather than restated here, so this test follows a
    change to the selector instead of silently continuing to assert the old
    one.
    """
    run = plan_step()["run"]
    marker = f"'{cron}') REQUESTED_SELECTOR="
    if marker in run:
        return run.split(marker, 1)[1].split()[0].rstrip(";")
    default = "*)           REQUESTED_SELECTOR="
    return run.split(default, 1)[1].split()[0].rstrip(";")


def test_the_weekly_covers_every_family_the_nightly_misses():
    """The point of the selector, asserted against the planner rather than
    against the string: no enabled build-chain cell may be left with no
    scheduled run at all."""
    weekly = {cell["id"] for cell in planned("--selector", scheduled_selector(WEEKLY))}
    nightly = {cell["id"] for cell in planned("--selector", scheduled_selector(NIGHTLY))}
    manifest = yaml.safe_load(
        (ROOT / "manifests" / "package-builds.yaml").read_text(encoding="utf-8")
    )
    enabled = {
        cell["id"] for cell in manifest["native_builds"]
        if cell.get("enabled", True) is not False
    }
    assert enabled - (weekly | nightly) == set()
    # And it is genuinely wider than the nightly, or it would add nothing.
    assert weekly - nightly


def test_the_two_schedules_do_not_share_a_concurrency_group():
    """Nine hours apart, but a nightly that overran into the weekly's slot
    would make the weekly QUEUE behind it and then be superseded -- the exact
    starvation the group exists to prevent, one level up."""
    group = spec()["concurrency"]["group"]
    assert "github.event.schedule" in group
    assert spec()["concurrency"]["cancel-in-progress"] == (
        "${{ github.event_name != 'schedule' }}"
    )


def test_a_schedule_is_still_never_cancelled():
    assert "github.event_name != 'schedule'" in spec()["concurrency"]["cancel-in-progress"]
