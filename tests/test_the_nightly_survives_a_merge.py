"""The scheduled full-family run must not share a cancel group with pushes.

`github.ref` is `refs/heads/main` for BOTH the nightly schedule and a push to
main, so one `cancel-in-progress` group covered both and every merge killed
the nightly. All three scheduled runs on record were cancelled; on
2026-08-21 the push run created at 13:51:50Z cancelled the 13:00:22Z nightly
at 13:52:27Z, 37 seconds later. On that day nothing on main completed:

    12:55:51 push      cancelled
    13:00:22 schedule  cancelled 13:52:27
    13:51:50 push      cancelled
    14:51:12 push      cancelled
    17:01:55 push      cancelled

That is the mechanism behind hummingbird's aarch64 index serving 1358
package names against x86_64's 7986 (#480) -- the slower arch is further
from finishing when the next merge lands, so it loses more work each time.
Nothing about the aarch64 leg itself is broken.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "package-factory.yml"


def concurrency() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["concurrency"]


def test_a_scheduled_run_is_never_cancelled_in_progress():
    assert "github.event_name != 'schedule'" in str(concurrency()["cancel-in-progress"])


def test_the_schedule_gets_a_group_that_no_push_can_share():
    """A distinct group is what actually protects it. Leaving cancel-in-progress
    conditional but the group shared would still let a push evict the nightly
    on some future change to the eviction rules."""
    group = concurrency()["group"]
    assert "github.event_name == 'schedule'" in group
    # The scheduled branch must not evaluate to github.ref, which is what a
    # push to main evaluates to. Asserted as "the two branches differ" rather
    # than by pinning a literal: the group is now keyed on which cron fired,
    # so that a nightly overrunning into the weekly's slot cannot starve it.
    scheduled, _, fallback = group.partition("||")
    assert "github.ref" not in scheduled
    assert "github.ref" in fallback


def test_non_schedule_runs_still_supersede_each_other():
    """PRs must keep cancelling: there a new commit genuinely obsoletes the
    old run, and dropping that would burn runners on dead revisions."""
    assert "github.ref" in concurrency()["group"]


def test_the_cancel_flag_is_an_expression_not_a_bare_true():
    """`cancel-in-progress: true` is precisely the bug -- pinned so a later
    edit cannot quietly restore it."""
    assert concurrency()["cancel-in-progress"] is not True


def test_two_nightlies_share_one_group_so_the_second_queues():
    """Not cancelling must not become 'run two full family builds at once'.
    Two runs of the SAME cron evaluate the group identically, and with
    cancel-in-progress false GitHub queues the newer one.

    The guard is that nothing run-unique appears in the group: github.run_id
    or github.sha would give every firing its own group and let an overrunning
    nightly overlap its own successor."""
    group = concurrency()["group"]
    for unique in ("github.run_id", "github.sha", "github.run_number", "github.run_attempt"):
        assert unique not in group, unique


def test_the_workflow_still_parses_and_keeps_its_triggers():
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = spec[True] if True in spec else spec["on"]
    assert "schedule" in triggers
    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers


def test_two_dispatches_of_different_cells_do_not_cancel_each_other():
    """Dispatches keep cancel-in-progress, so before the group distinguished
    them, dispatching one cell killed an unrelated cell dispatched minutes
    earlier on the same branch -- up to an hour of aarch64 build time
    discarded for nothing. Two different cells are independent work.

    Re-dispatching the SAME cell still cancels, because the key is identical;
    that one IS a supersede."""
    group = concurrency()["group"]
    assert "github.event_name == 'workflow_dispatch'" in group
    assert "github.event.inputs.cell" in group
    assert "github.event.inputs.selector" in group


def test_the_dispatch_key_still_separates_branches():
    """Two branches dispatching the same cell are still different work."""
    group = concurrency()["group"]
    dispatch = group.split("workflow_dispatch'", 1)[1].split("|| github.ref")[0]
    assert "github.ref" in dispatch
