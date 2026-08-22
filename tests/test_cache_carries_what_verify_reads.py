"""Whatever the verify path reads from the cell directory must be cached.

#472 trimmed the action cache to artifacts/ + action-result.json, on the
stated claim that verify-package-factory-cell.sh reads "$out/artifacts and
nothing else". That is true for the rpm and deb branches and WRONG for Arch:
the pkg.tar.zst branch passes "$out/package-info.txt" to
validate-built-arch-package.py.

It stayed latent because Arch cells kept missing the cache, and a fresh build
writes the file. Once #477 made action keys stable across history rewrites
they began hitting, and every Arch hit died:

    FileNotFoundError: .../.factory/<cell>/package-info.txt

(tideforge-wayland-protocols-arch-x86_64, run 32551508574 — a canary cell,
which is exactly what canary cells exist to catch.)

This test derives the requirement from the verify script instead of listing
the file, so a future branch that reads another build product fails here
rather than in CI on whichever cell happens to hit first.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CELL = ROOT / ".github" / "workflows" / "package-factory-cell.yml"
VERIFY = ROOT / "scripts" / "verify-package-factory-cell.sh"

# Written by verify itself before it is read, so it never needs restoring.
SELF_WRITTEN = {"smoke.sh"}


def out_paths_verify_reads() -> set[str]:
    text = VERIFY.read_text(encoding="utf-8")
    names = set()
    for match in re.findall(r'\$out/([A-Za-z0-9_.-]+)', text):
        names.add(match)
    return names - SELF_WRITTEN


def cache_path_lists() -> list[set[str]]:
    """The ACTION-CACHE path lists, parsed from the workflow rather than
    regexed out of it.

    An earlier version of this test regexed every `path: |` block and
    intersected them, which silently folded in the upload-artifact list
    (artifacts/, metadata/) and made the assertion meaningless. Selecting by
    the step's `uses` is what actually identifies a cache step.
    """
    workflow = yaml.safe_load(CELL.read_text(encoding="utf-8"))
    lists = []
    for step in workflow["jobs"]["build"]["steps"]:
        uses = str(step.get("uses") or "")
        if "tideforge-action-cache" not in uses and "actions/cache" not in uses:
            continue
        raw = (step.get("with") or {}).get("path")
        if not raw:
            continue
        lists.append({
            line.strip().rsplit("/", 1)[-1]
            for line in str(raw).strip().splitlines()
            if line.strip()
        })
    return lists


def cached_names() -> set[str]:
    lists = cache_path_lists()
    assert lists, "no action-cache path lists found"
    return set.intersection(*lists)


def test_verify_reads_at_least_the_known_build_products():
    """Guards the derivation itself: if this regexes nothing, the test below
    passes vacuously and the bug walks straight back in."""
    assert {"artifacts", "package-info.txt"} <= out_paths_verify_reads()


def test_every_build_product_verify_reads_is_cached():
    missing = out_paths_verify_reads() - cached_names()
    assert not missing, missing


def test_restore_and_save_lists_agree():
    """Restoring a path that is never saved, or saving one never restored,
    both silently do nothing — and a fix applied to only one of the two lists
    looks correct in review."""
    lists = cache_path_lists()
    assert len(lists) >= 2, lists
    assert len({tuple(sorted(x)) for x in lists}) == 1, lists
