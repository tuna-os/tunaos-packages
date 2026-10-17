"""A push to main must not race the nightly for the same partial artifact.

Both the `schedule` and `push: branches: [main]` triggers plan from the same
catalog, and their concurrency groups differ BY CONSTRUCTION -- a schedule
gets `package-factory-schedule-<cron>`, a push gets `package-factory-<ref>`
-- so neither cancels the other. Before this was fixed both could therefore
plan the same full build-chain cell and run it concurrently for 4.5 h each,
and both upload `<cell>-partial` with `overwrite: true`. Whichever finished
last won the artifact name the other run's continuation shards resume from.

Observed live on 2026-08-25: merging #512 started a push run whose build-0
included `hummingbird-x86_64` at 11:26:51; the nightly cron fired at 12:58:54
and planned the same cell. Up to 13.5 h of runner time to end up where one
run alone would have left us.

The fix is to plan a push to main in CANARY form, like the pull_request and
merge_group events -- which is not a weakening: a push to main is the merge
of a PR that already ran this exact selection through the merge queue.
Build-chain cells still build on a push, bounded to their canary tiers, so
an infra change is still exercised; only the long chains are left to the
schedule designed to carry them.
"""
from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "package-factory.yml"


def plan_step() -> str:
    body = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in body["jobs"]["plan"]["steps"]:
        if step.get("id") == "plan":
            return step["run"]
    raise AssertionError("package-factory.yml has no plan step with id: plan")


def test_a_push_to_main_plans_in_canary_form() -> None:
    run = plan_step()
    case_lines = [line for line in run.splitlines()
                  if "--canary-common" in line and "args+=" in line]
    assert case_lines, "nothing passes --canary-common any more"
    guard = case_lines[0]
    assert "push" in guard.split(")")[0], (
        "a push to main plans full build chains, so merging anything can "
        "start a 4.5 h chain that races the nightly for the same "
        "<cell>-partial artifact name. Observed live after #512 merged."
    )


def test_the_schedule_still_plans_full_chains() -> None:
    """The guard must not swallow the one event that NEEDS the long chains.

    Adding `schedule` to the canary list would be a silent catastrophe: every
    nightly would build a handful of canary tiers, report green, and the
    hummingbird chain would never converge again.
    """
    run = plan_step()
    guard = next(line for line in run.splitlines()
                 if "--canary-common" in line and "args+=" in line)
    selector = guard.split(")")[0]
    assert "schedule" not in selector, (
        "the nightly would plan canary tiers instead of the full chain, and "
        "the desktop chain would silently stop converging"
    )


def test_the_two_triggers_cannot_share_a_concurrency_group() -> None:
    """Why the canary form is the fix rather than a shared group.

    This pins the PREMISE. If someone later makes the groups identical the
    race is solved a different way and this file's reasoning needs revisiting
    -- better to fail here and be re-read than to leave a stale rationale.
    """
    group = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["concurrency"]["group"]
    assert "schedule" in group and "github.ref" in group, (
        "the concurrency group no longer distinguishes schedule from push; "
        "re-check whether the canary-form guard is still the right fix"
    )


def test_the_flag_actually_removes_the_full_chain() -> None:
    """Pin the BEHAVIOUR, because the text assertions above are too weak.

    `canary_common` only takes effect on the infra path -- changed files
    supplied AND `affected_formats` returning None because a COMMON_INPUT was
    touched. On every other path the planner returns before reaching it. So a
    test that only reads the workflow's event list passes just as happily
    when the flag is inert, which is the exact failure this repository keeps
    re-learning: a check that examines nothing.

    #512's own changed set is the fixture, since it is what produced the race.
    """
    import importlib.util
    import json
    import subprocess
    import sys

    spec = importlib.util.spec_from_file_location(
        "plan_package_factory", ROOT / "scripts" / "plan-package-factory.py")
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)

    # A COMMON_INPUT is what makes a change infra-wide; assert the premise
    # rather than trusting it, so a rename of these paths fails loudly here.
    infra = sorted(planner.COMMON_INPUTS)[:1]
    assert planner.affected_formats(set(infra)) is None, (
        "COMMON_INPUTS no longer marks a change as infra-wide, so the "
        "canary guard can never engage"
    )

    def plan(canary: bool) -> set[str]:
        changed = ROOT / ".git" / "canary-guard-changed.txt"
        changed.write_text("\n".join(infra) + "\n", encoding="utf-8")
        try:
            argv = [sys.executable, str(ROOT / "scripts" / "plan-package-factory.py"),
                    "--changed-files", str(changed)]
            if canary:
                argv.append("--canary-common")
            out = subprocess.run(argv, capture_output=True, text=True,
                                 check=True, cwd=ROOT).stdout
            ids = set()
            for matrix in json.loads(out)["matrices"]:
                ids |= {c["id"] for c in json.loads(matrix)["include"]}
            return ids
        finally:
            changed.unlink(missing_ok=True)

    full = {"hummingbird-x86_64", "hummingbird-aarch64"}
    assert full & plan(canary=False), (
        "fixture no longer reproduces the race: an infra change must plan the "
        "full chains without the guard, or this test proves nothing"
    )
    assert not (full & plan(canary=True)), (
        "--canary-common leaves the full hummingbird chain in the plan, so a "
        "push to main still races the nightly for <cell>-partial"
    )
