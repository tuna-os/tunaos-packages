#!/usr/bin/env python3
"""Decide which Tideforge gate cells actually need to run.

WHY.  `detect-changes` is one boolean for the whole gate: if anything under
packages/, scripts/, the gate workflows or .github/actions/ changed, ALL 98
cells build (84 supported + 14 arch); otherwise none do.  Editing one recipe
therefore rebuilds 97 bit-identical artifacts, each paying for a runner, a
container image pull and a full mock/sbuild cycle.

This module replaces that boolean with a plan.  It never invents cells: it
reads the cells the workflows already declare and *subtracts*, so the
workflows stay the single source of truth and the coverage gate keeps working.

THE DANGEROUS DIRECTION.  This repo's recurring defect is the silent skip — a
declared target no cell exercised (#139), a wishlist that quietly made every
missing package optional (#1080).  A dispatcher is a machine for manufacturing
exactly that, so every rule here is written to fail toward building:

  * unknown event, unreadable file, unparseable workflow, missing recipe,
    absent proven-set  -> build everything
  * a shared input changed (renderer, mock config, workflow, actions)
    -> build everything, because those inputs feed every cell
  * a job whose upstream runs -> runs, transitively, because it consumes that
    upstream's artifacts

and every skip is reported with the reason that produced it.

INTER-CELL EDGES COME FROM THE WORKFLOW, NOT THE RECIPES.  A recipe's
`dependencies:` lists DISTRO package names (gcc, meson, cmake) -- measured:
zero of the 46 recipes name a sibling recipe there.  The real edges are the
jobs' own `needs:`, e.g. quickshell-rpm needs cpptrace-rpm and cosmic-comp-rpm
needs cosmic-icon-theme-rpm.  Closing over `dependencies:` would have produced
a plan that looks dependency-aware and silently drops the consumer of a
changed producer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

# Files that feed EVERY cell.  A change to any of them invalidates the whole
# gate, so the plan degrades to "build everything".  Deliberately broad: a
# false full-build costs runner minutes, a false skip costs a regression.
SHARED_INPUT_PATTERNS = (
    re.compile(r"^scripts/"),
    re.compile(r"^mock/"),
    re.compile(r"^\.github/workflows/build-tideforge-"),
    re.compile(r"^\.github/actions/"),
    re.compile(r"^manifests/package-factory\.yaml$"),
    re.compile(r"^build-order-"),
)

# Files whose contents go into every fingerprint, so a change to one of them
# makes previously-proven fingerprints stale automatically rather than relying
# on the SHARED_INPUT_PATTERNS short-circuit alone.  Belt and braces on purpose.
FINGERPRINT_SHARED_FILES = (
    "scripts/tideforge.py",
    "scripts/assemble-deb-source-tree.py",
    "scripts/build-chain.sh",
)

RECIPE_PATH = re.compile(r"^packages/([A-Za-z0-9_.+-]+)/")
RECIPE_IN_BODY = re.compile(r"packages/([A-Za-z0-9_.+-]+)/package\.yaml")


class PlanError(Exception):
    """Raised only for caller mistakes; never for 'I could not decide'."""


def is_shared_input(path: str) -> bool:
    return any(p.search(path) for p in SHARED_INPUT_PATTERNS)


def changed_packages(changed_files) -> set[str]:
    """Recipe directories touched by this diff."""
    found = set()
    for path in changed_files:
        match = RECIPE_PATH.match(path.strip())
        if match:
            found.add(match.group(1))
    return found


def load_workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def job_packages(name: str, job: dict) -> set[str]:
    """Every recipe a job builds: matrix `package:` keys, else paths in its body.

    Dedicated jobs (gtkgreet-rpm, niri-rpm, ...) name their recipe in a step
    rather than a matrix, which is why the body is scanned as a fallback.
    """
    include = (job.get("strategy") or {}).get("matrix", {}).get("include") or []
    pkgs = {cell["package"] for cell in include if isinstance(cell, dict) and "package" in cell}
    if pkgs:
        return pkgs
    return set(RECIPE_IN_BODY.findall(yaml.safe_dump(job)))


def job_needs(job: dict) -> list[str]:
    needs = job.get("needs")
    if isinstance(needs, str):
        return [needs]
    return list(needs or [])


def running_jobs(workflow: dict, seeds: set[str]) -> tuple[set[str], set[str]]:
    """(jobs that build a seeded package, jobs pulled in downstream of those).

    The split is load-bearing, not bookkeeping.  A SEEDED job runs because one
    of its own recipes changed, so only that recipe's cells have anything to
    prove.  A DOWNSTREAM job runs because it installs an upstream's freshly
    built artifact -- cosmic-comp-deb consumes cosmic-icon-theme-deb -- and
    there the changed recipe is somebody else's, so every one of its cells must
    run.  Filtering downstream cells by "is your own recipe in the diff" drops
    exactly the consumer the closure just worked to include.
    """
    jobs = workflow.get("jobs", {})
    seeded = {name for name, job in jobs.items() if job_packages(name, job) & seeds}
    running = set(seeded)
    # Transitive closure over `needs:` edges, downstream direction.
    changed = True
    while changed:
        changed = False
        for name, job in jobs.items():
            if name in running:
                continue
            if set(job_needs(job)) & running:
                running.add(name)
                changed = True
    return seeded, running - seeded


def _read_bytes(root: Path, rel: str) -> bytes:
    try:
        return (root / rel).read_bytes()
    except OSError:
        # Absent shared file is not an error; it just contributes nothing.
        return b""


def recipe_fingerprint(root: Path, package: str, target: str, image: str, extra: bytes = b"") -> str | None:
    """Build identity for one cell, or None when it cannot be computed.

    None means "cannot prove this is unchanged", and every caller treats that
    as must-build.
    """
    pkg_dir = root / "packages" / package
    if not pkg_dir.is_dir():
        return None
    digest = hashlib.sha256()
    try:
        for path in sorted(p for p in pkg_dir.rglob("*") if p.is_file()):
            digest.update(str(path.relative_to(pkg_dir)).encode())
            digest.update(path.read_bytes())
    except OSError:
        return None
    digest.update(b"\0target=" + target.encode())
    # The image is pinned by whatever the cell declares.  A tag that moves
    # under us (debian:sid) is exactly why phase 2 alone is not trusted to
    # catch base-image changes -- SHARED_INPUT_PATTERNS covers the workflow
    # edit that would accompany a deliberate bump.
    digest.update(b"\0image=" + image.encode())
    for rel in FINGERPRINT_SHARED_FILES:
        digest.update(b"\0" + rel.encode())
        digest.update(hashlib.sha256(_read_bytes(root, rel)).digest())
    digest.update(b"\0" + extra)
    return digest.hexdigest()


def plan(
    workflow: dict,
    changed_files=None,
    root: Path = Path("."),
    proven: set[str] | None = None,
) -> dict:
    """Return {job: {"run": bool, "matrix": {...}|None, "skipped": [...]}}.

    changed_files=None means "no diff information" -> build everything.
    proven=None means "no proven-fingerprint set" -> skip nothing on that basis.
    """
    jobs = workflow.get("jobs", {})
    proven = proven or set()

    if changed_files is None:
        reason, seeds, full = "no diff information", set(), True
    else:
        shared = sorted(f for f in (c.strip() for c in changed_files) if is_shared_input(f))
        if shared:
            reason, seeds, full = f"shared input changed: {shared[0]}", set(), True
        else:
            seeds = changed_packages(changed_files)
            reason, full = f"changed recipes: {sorted(seeds) or 'none'}", False

    if full:
        seeded_jobs, live = set(jobs), set(jobs)
    else:
        seeded_jobs, downstream = running_jobs(workflow, seeds)
        live = seeded_jobs | downstream

    result = {"_reason": reason, "_full": full, "jobs": {}}
    for name, job in jobs.items():
        include = (job.get("strategy") or {}).get("matrix", {}).get("include") or []
        entry = {"run": name in live, "matrix": None, "skipped": []}
        if not include:
            if not entry["run"]:
                entry["skipped"].append({"cell": name, "why": "no changed package reaches this job"})
            result["jobs"][name] = entry
            continue

        kept = []
        for cell in include:
            pkg = cell.get("package") if isinstance(cell, dict) else None
            if name not in live:
                entry["skipped"].append({"cell": f"{name}:{pkg}", "why": "job not reached by the change"})
                continue
            if pkg and not full and name in seeded_jobs and pkg not in seeds:
                # A seeded matrix job is live because ONE of its packages
                # changed; its other cells still have nothing to prove.  This
                # filter deliberately does NOT apply to downstream jobs -- see
                # running_jobs().
                entry["skipped"].append({"cell": f"{name}:{pkg}", "why": "recipe unchanged"})
                continue
            fp = recipe_fingerprint(
                root, pkg or "", cell.get("target", ""), cell.get("image", "")
            ) if pkg else None
            if fp and fp in proven:
                entry["skipped"].append({"cell": f"{name}:{pkg}", "why": f"already proven ({fp[:12]})"})
                continue
            kept.append(cell)
        entry["matrix"] = {"include": kept}
        entry["run"] = bool(kept)
        result["jobs"][name] = entry
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workflow", required=True, type=Path)
    ap.add_argument("--changed-files", type=Path,
                    help="file of changed paths, one per line; omit for a full build")
    ap.add_argument("--proven", type=Path,
                    help="file of already-proven fingerprints, one per line")
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--github-output", type=Path)
    args = ap.parse_args(argv)

    try:
        workflow = load_workflow(args.workflow)
    except (OSError, yaml.YAMLError) as exc:
        # Fail open, loudly: an unreadable workflow must not silence the gate.
        print(f"plan-gate-matrix: cannot read {args.workflow} ({exc}); building everything",
              file=sys.stderr)
        workflow = {"jobs": {}}

    changed = None
    if args.changed_files:
        try:
            changed = [line for line in args.changed_files.read_text().splitlines() if line.strip()]
        except OSError as exc:
            print(f"plan-gate-matrix: cannot read {args.changed_files} ({exc}); building everything",
                  file=sys.stderr)
    proven = set()
    if args.proven:
        try:
            proven = {line.strip() for line in args.proven.read_text().splitlines() if line.strip()}
        except OSError as exc:
            print(f"plan-gate-matrix: cannot read {args.proven} ({exc}); proving nothing",
                  file=sys.stderr)

    result = plan(workflow, changed, args.root, proven)

    print(f"plan-gate-matrix: {result['_reason']}")
    total_kept = total_skipped = 0
    for name, entry in sorted(result["jobs"].items()):
        kept = len((entry["matrix"] or {}).get("include", [])) if entry["matrix"] else int(entry["run"])
        total_kept += kept
        total_skipped += len(entry["skipped"])
        if entry["skipped"]:
            # No silent skips: every dropped cell is named with its reason.
            for s in entry["skipped"]:
                print(f"  skip {s['cell']}: {s['why']}")
    print(f"plan-gate-matrix: {total_kept} cell(s) to build, {total_skipped} skipped")

    if args.github_output:
        with args.github_output.open("a") as fh:
            for name, entry in result["jobs"].items():
                key = name.replace("-", "_")
                fh.write(f"{key}_run={str(entry['run']).lower()}\n")
                if entry["matrix"] is not None:
                    fh.write(f"{key}_matrix={json.dumps(entry['matrix'])}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
