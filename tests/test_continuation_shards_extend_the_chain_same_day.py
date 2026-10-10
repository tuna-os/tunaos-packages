"""One nightly must bank more than one budget of chain, without new machinery.

A full desktop family (1,248 packages for hummingbird) is ~44h of mock time.
The soft deadline ends a cell cleanly at ~4.5h; before this, the next attempt
was tomorrow's schedule — ~5.5h of chain per DAY. The build-1/build-2 shards
existed only as >200-cell overflow and had never once run.

Chaining them and seeding CONTINUATION copies of each full-chain cell triples
the chain hours per run, built entirely from mechanisms that already exist
and are already tested: the continuation's partial artifact is NAMED by
base_id so the resume step restores the previous shard's upload, and the
input-derived action key means a shard that FINISHES the chain makes every
later continuation cache-hit and skip.

The failure modes pinned here are the quiet ones:
  * shards running in parallel again (a continuation racing its predecessor
    for the same partial),
  * the gate passing while an occupied continuation shard failed (a green
    run whose extra hours silently died),
  * continuations leaking onto bounded cells (canary, TIERS-scoped,
    tideforge), which have no partial progress to resume.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLANNER = ROOT / "scripts" / "plan-package-factory.py"
FACTORY = ROOT / ".github" / "workflows" / "package-factory.yml"
CELL = ROOT / ".github" / "workflows" / "package-factory-cell.yml"


def plan(selector: str) -> list[list[dict]]:
    proc = subprocess.run(
        [sys.executable, str(PLANNER), "--selector", selector],
        capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr[-1500:]
    payload = json.loads(proc.stdout)
    return [json.loads(m)["include"] for m in payload["matrices"]]


def test_full_chain_cells_continue_into_chained_shards():
    shards = plan("family=hummingbird-desktops")
    base_ids = {c["id"] for c in shards[0]}
    assert base_ids, "planner selected nothing — the fixture family moved"
    for index, suffix in ((1, "-c1"), (2, "-c2")):
        got = {c["id"]: c.get("base_id") for c in shards[index]}
        for base in base_ids:
            assert f"{base}{suffix}" in got, (
                f"shard {index} lacks a continuation for {base}"
            )
            assert got[f"{base}{suffix}"] == base, (
                "continuation does not carry base_id — the resume step will "
                "look for a partial artifact that nothing uploads"
            )


def plan_args(*args: str) -> list[list[dict]]:
    proc = subprocess.run(
        [sys.executable, str(PLANNER), *args],
        capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr[-1500:]
    payload = json.loads(proc.stdout)
    return [json.loads(m)["include"] for m in payload["matrices"]]


def test_bounded_cells_do_not_continue():
    """A tideforge cell builds one package; nothing to resume.

    Selected via --cell rather than engine=tideforge: the engine selector
    matches ~289 cells, so shards 1-2 legitimately hold OVERFLOW there —
    which the first version of this test misread as leaked continuations.
    """
    shards = plan_args("--cell", "tideforge-libunwind-devel-el10-x86_64")
    assert shards[0], "the fixture cell id no longer exists — pick another"
    assert not shards[1] and not shards[2], (
        "a single-package cell grew continuations"
    )


def test_canary_cells_do_not_continue():
    """Canaries are bounded by construction: canary_cells() rewrites the id
    to -canary and sets tiers=canary_tiers, and a cell with TIERS set asked
    for a bounded slice — nothing to continue."""
    changed = ROOT / ".factory-test-changed"  # a path affecting no single format
    changed.write_text(".github/workflows/package-factory-cell.yml\n")
    try:
        shards = plan_args("--canary-common", "--changed-files", str(changed))
    finally:
        changed.unlink()
    canaries = [c for c in shards[0] if c["id"].endswith("-canary")]
    assert canaries, "canary-common selected no canaries — fixture moved"
    # Full-chain cells in the same plan MAY continue (a continuation of a
    # completed cell cache-hits its action key and skips in seconds, so it is
    # a cheap no-op). The property that matters is narrower: a TIERS-bounded
    # canary must never be extended past the slice it asked for.
    canary_ids = {c["id"] for c in canaries}
    leaked = [c for shard in shards[1:] for c in shard
              if c.get("base_id") in canary_ids]
    assert not leaked, f"bounded canaries grew continuations: {[c['id'] for c in leaked]}"


def _jobs() -> dict:
    return yaml.safe_load(FACTORY.read_text())["jobs"]


def test_the_shards_are_chained_not_parallel():
    jobs = _jobs()
    assert "build-0" in jobs["build-1"]["needs"], (
        "build-1 no longer waits for build-0 — a continuation racing its "
        "predecessor for the same partial artifact"
    )
    assert "build-1" in jobs["build-2"]["needs"]


def test_the_shard_conditions_read_occupancy_not_overflow_count():
    jobs = _jobs()
    for name, key in (("build-1", "count_1"), ("build-2", "count_2")):
        cond = jobs[name].get("if", "")
        assert key in cond, (
            f"{name} is still gated on the >200 overflow count; continuation "
            "cells would put work there that the condition never runs"
        )
        assert "!cancelled()" in cond, (
            f"{name} skips when any build-0 cell fails — one unrelated red "
            "cell throwing away the day's remaining chain hours"
        )


def test_the_partial_is_named_by_base_id_and_overwritable():
    text = CELL.read_text()
    assert "${{ matrix.base_id || matrix.id }}-partial" in text, (
        "the partial artifact is no longer named by base_id; continuation "
        "shards will upload disjoint partials and resume none of them"
    )
    steps = yaml.safe_load(text)["jobs"]["build"]["steps"]
    partial = next(s for s in steps
                   if "-partial" in str((s.get("with") or {}).get("name", "")))
    assert partial["with"].get("overwrite") is True, (
        "without overwrite the second shard's partial upload fails on the "
        "duplicate name and the third shard resumes stale work"
    )


def test_the_restore_searches_by_base_id():
    text = CELL.read_text()
    assert "--cell-id '${{ matrix.base_id || matrix.id }}'" in text


def _gate_script() -> str:
    """Find the continuation gate command among other orchestration steps."""
    steps = _jobs()["gate"]["steps"]
    return next(s["run"] for s in steps if "gate-shard-results.py" in s.get("run", ""))


def _chain(cell_id: str) -> tuple[str, str, str]:
    """The three matrices the planner emits for one full-chain cell."""
    return (
        json.dumps({"include": [{"id": cell_id}]}),
        json.dumps({"include": [{"id": f"{cell_id}-c1", "base_id": cell_id}]}),
        json.dumps({"include": [{"id": f"{cell_id}-c2", "base_id": cell_id}]}),
    )


def _run_gate(**env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _gate_script()], env=env, cwd=ROOT,
        capture_output=True, text=True, timeout=60,
    )


def test_the_gate_fails_when_the_last_continuation_dies():
    """The case the strictness exists for: nothing runs after build-2, so
    nothing supersedes it, and its failure is the chain extension dying with
    the day's remaining hours."""
    m0, m1, m2 = _chain("gnome50-el10-x86_64")
    proc = _run_gate(
        PATH=os.environ["PATH"], PLAN="success", COUNT="1",
        COUNT_0="1", COUNT_1="1", COUNT_2="1",
        SHARD_0="success", SHARD_1="success", SHARD_2="failure",
        MATRIX_0=m0, MATRIX_1=m1, MATRIX_2=m2,
    )
    assert proc.returncode != 0, (
        "gate passed with a failed final continuation — a green run whose "
        "chain extension silently died\n" + proc.stdout
    )


def test_the_gate_forgives_a_shard_a_later_continuation_carried():
    """tunaos-packages#684. Run 33840428161: build-0 failed on a transient
    upstream fetch, build-1 resumed its partial and built through it,
    build-2 finished. The chain completed; the gate used to call it red."""
    m0, m1, m2 = _chain("gnome50-el10-x86_64")
    proc = _run_gate(
        PATH=os.environ["PATH"], PLAN="success", COUNT="1",
        COUNT_0="1", COUNT_1="1", COUNT_2="1",
        SHARD_0="failure", SHARD_1="success", SHARD_2="success",
        MATRIX_0=m0, MATRIX_1=m1, MATRIX_2=m2,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_gate_ignores_empty_shards():
    proc = _run_gate(
        PATH=os.environ["PATH"], PLAN="success", COUNT="2",
        COUNT_0="2", COUNT_1="0", COUNT_2="0",
        SHARD_0="success", SHARD_1="skipped", SHARD_2="skipped",
        MATRIX_0=json.dumps({"include": [{"id": "a"}, {"id": "b"}]}),
        MATRIX_1='{"include":[]}', MATRIX_2='{"include":[]}',
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_deferred_build_still_uploads_its_partial():
    """The linchpin. A deferred chain exits 0, so its outcome is `success` —
    which the partial upload's allowlist (failure/cancelled) did not cover.
    Without this clause a deferred shard uploads nothing, the next shard
    restores the previous DAY's partial, and the continuation design loses
    exactly the hours it exists to bank. Caught by reading the seam before
    the first live run, the same class as SOURCE_DATE_EPOCH in #464: the
    units were right and the seam was wrong."""
    steps = yaml.safe_load(CELL.read_text())["jobs"]["build"]["steps"]
    partial = next(s for s in steps
                   if "-partial" in str((s.get("with") or {}).get("name", "")))
    cond = partial.get("if", "")
    assert "chain_deferred" in cond, (
        "the partial upload ignores deferral; continuation shards will "
        "resume stale work"
    )
    # Still an allowlist: a cache-hit (outcome `skipped`) must not upload —
    # that regression cost a redundant ~500MB copy per green cell once.
    assert "outcome == 'failure'" in cond and "!=" not in cond


def test_the_overwrite_has_the_permission_it_needs():
    """upload-artifact implements overwrite by DELETING the existing artifact
    first, and the delete API requires `actions: write`. Both the cell
    workflow and its caller declared `actions: read` — the second shard's
    partial upload would have 403'd at exactly the hand-off the whole design
    depends on, at runtime, tonight. A called workflow cannot request more
    than its caller grants, so BOTH files must say write."""
    for path in (CELL, FACTORY):
        perms = yaml.safe_load(path.read_text()).get("permissions", {})
        assert perms.get("actions") == "write", (
            f"{path.name}: actions permission is {perms.get('actions')!r}; "
            "the shared-partial overwrite will be rejected"
        )
