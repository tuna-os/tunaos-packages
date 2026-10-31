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

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_flag_actually_removes_the_full_chain(tmp_path: pathlib.Path) -> None:
    """Exercise the planner's full-chain and canary behavior through its CLI.

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

    changed = tmp_path / "canary-guard-changed.txt"
    changed.write_text("\n".join(infra) + "\n", encoding="utf-8")

    def plan(canary: bool) -> set[str]:
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

    full = {"hummingbird-x86_64", "hummingbird-aarch64"}
    assert full & plan(canary=False), (
        "fixture no longer reproduces the race: an infra change must plan the "
        "full chains without the guard, or this test proves nothing"
    )
    assert not (full & plan(canary=True)), (
        "--canary-common leaves the full hummingbird chain in the plan, so a "
        "push to main still races the nightly for <cell>-partial"
    )
