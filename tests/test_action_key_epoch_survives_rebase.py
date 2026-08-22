"""The action-key epoch must not change when history is rewritten.

`package-factory-cell.yml` feeds a git-derived timestamp into the action key
as SOURCE_DATE_EPOCH. It used `%ct` -- the COMMITTER date -- which a commit
object gets afresh on every history rewrite even when the tree is unchanged:
rebase, cherry-pick, `--amend`, and squash-merge (this repo's merge
convention).

Measured on a byte-identical pair: run 32540188656 (fd90915) and run
32541649752 (1e9cdfb) differ only by a `git cherry-pick`, and every quickshell
cell rebuilt from scratch in both -- ~15 minutes per cell, paid twice for the
same bytes. Since a squash-merge rewrites the commit too, the first run on
main after any merge re-derives a fresh epoch for every recipe the PR touched.

`%at` (author date) is preserved by cherry-pick, rebase and amend, so an
unchanged recipe keeps its key. See #477.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CELL = ROOT / ".github" / "workflows" / "package-factory-cell.yml"


def workflow() -> str:
    return CELL.read_text(encoding="utf-8")


def epoch_lines() -> list[str]:
    return [ln.strip() for ln in workflow().splitlines() if "epoch=$(git log" in ln]


def test_both_engines_derive_an_epoch():
    """tideforge and build-chain each have their own derivation; a fix that
    only lands on one leaves the other invalidating on every rewrite."""
    assert len(epoch_lines()) == 2, epoch_lines()


def test_no_epoch_uses_committer_date():
    assert not [ln for ln in epoch_lines() if "%ct" in ln], epoch_lines()


def test_every_epoch_uses_author_date():
    assert all("--format=%at" in ln for ln in epoch_lines()), epoch_lines()


def test_the_epoch_still_reaches_the_action_key():
    """A stable epoch is pointless if it stops being an input -- the key would
    then ignore it entirely rather than track it stably."""
    assert "--source-date-epoch" in workflow()


def test_the_reason_is_recorded_next_to_the_change():
    """%at over %ct looks arbitrary without the rewrite rationale, and the
    obvious 'fix' for a reader is to put %ct back."""
    text = workflow()
    anchor = text.index("--format=%at")
    preamble = text[max(0, anchor - 1200):anchor]
    assert "#477" in preamble
    assert re.search(r"author", preamble, re.I)
