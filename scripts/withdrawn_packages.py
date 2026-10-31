"""Load and match manifests/withdrawn-packages.yaml.

A served package older than upstream's build shadows upstream's fix under
this factory's dnf `priority: 5` (tunaos-packages#525) -- and nothing
performed the remedy, because the remedy looked like it had to delete
objects from a live, publicly-served bucket, which is exactly the shape of
mistake #124 / INCIDENT-repo-wipe-gnome exists to prevent.

It does not have to. `createrepo_c -x PATTERN` excludes a file from the
generated repodata without touching the file -- the RPM stays in the
synced-down tree, publish-rpm-wave.sh's NEVER SHRINK guard (which counts
files on disk) never sees a withdrawal happen, and deleting the manifest
entry below un-withdraws the package on the very next publish. No new
destructive capability, no new credential, nothing a contributor runs
against production: just a glob a publisher already knew how to take.

This module is the one place that knows the manifest's shape, imported by
both the producer-side report (scripts/check-upstream-shadowing.py, so a
withdrawal already recorded here does not re-fail the run every time upstream
moves again) and the consumer-side plan
(scripts/plan-withdrawn-excludes.py, which turns entries into the globs
publish-rpm-wave.sh passes to createrepo_c). A third copy of the matching
rule -- name + evr, not bare name, see the manifest's own header -- would be
a third thing to keep correct.
"""
from __future__ import annotations

import pathlib
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "manifests" / "withdrawn-packages.yaml"
SCHEMA = 1


def load(path: pathlib.Path = DEFAULT_MANIFEST) -> dict[str, list[dict[str, Any]]]:
    """target name -> its list of {name, evr, reason} withdrawn entries.

    Missing file or wrong/missing schema reads as "nothing withdrawn" rather
    than raising -- a manifest that has not been created yet, or a merge
    conflict that clobbered it, must not make every publish refuse to index
    (--exclude-file would then have nothing to read, which is the same as
    not passing it at all).
    """
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if data.get("schema") != SCHEMA:
        return {}
    targets = data.get("targets")
    return targets if isinstance(targets, dict) else {}


def is_withdrawn(targets: dict[str, list[dict[str, Any]]], target: str,
                  name: str, evr: str) -> bool:
    """Whether this EXACT (name, evr) has a recorded withdrawal for target.

    Exact-evr, not name-only: the gap measurement already stops asking the
    factory to rebuild a name upstream ships, so a future build of this name
    at a DIFFERENT evr -- upstream dropping it, or us catching back up -- is
    a separate decision and must not inherit an old entry's withdrawal.
    """
    for entry in targets.get(target, []):
        if not isinstance(entry, dict):
            continue
        if entry.get("name") == name and str(entry.get("evr")) == evr:
            return True
    return False


def excludes_for(targets: dict[str, list[dict[str, Any]]],
                  target: str) -> list[str]:
    """createrepo_c `-x` glob patterns for every entry declared under target.

    One leading `*` absorbs any path prefix createrepo_c's directory walk
    reports (a flat repo root or a nested --subdir, see publish-rpm-wave.sh);
    one trailing `*.rpm` absorbs the arch suffix, so one entry excludes a
    name-evr pair on every architecture that published it, not just the one
    scripts/check-upstream-shadowing.py measured.
    """
    return sorted(
        f"*{entry['name']}-{entry['evr']}.*.rpm"
        for entry in targets.get(target, [])
        if isinstance(entry, dict) and entry.get("name") and entry.get("evr")
    )
