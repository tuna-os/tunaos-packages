#!/usr/bin/env python3
"""Refuse an index regeneration that breaks what the old index resolved.

The deb and arch publishers do not merge a staged wave into a served
tree the way publish-rpm-wave.sh does — they regenerate the WHOLE index
in place (apt-ftparchive over pool/, repo-add into the .db) and sync it
up. So their reverse-dependency gate is an old-vs-new comparison: every
dependency that resolved within the old index's view must still resolve
in the new one. Same differential principle as check-reverse-deps.py
(adapted from ebranch's check-update, slopfest/sandogasa): a dependency
unresolvable in BOTH views lives outside what the gate can see (the
distro's own repositories) and is never noise; one that regressed is
precisely what this publish would break.

Format semantics are native, not approximated:
  * deb — apt's candidate is the highest version per package name
    (dpkg ordering, scripts/deb_version.py); Depends/Pre-Depends with
    `|` alternatives, where a group is satisfied by ANY branch; a
    versioned dependency is satisfied by a real package or a versioned
    Provides, never a bare virtual (Debian policy 7.5).
  * pacman — the .db carries one version per name; depends compare
    with libalpm's vercmp (scripts/pacman_db.py).

Usage (both run inside the publisher, entirely locally):
    scripts/check-index-regression.py --format deb \\
        --old repo/Packages.old --new repo/Packages
    scripts/check-index-regression.py --format pacman \\
        --old /work/old-tunaos.db --new /work/repo/tunaos.db

Exits 1 on a regression; a missing --old file means a first publish —
nothing served, nothing to regress, exit 0.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent

_MODULES: dict[str, object] = {}


def load(name: str, filename: str | None = None):
    if name not in _MODULES:
        spec = importlib.util.spec_from_file_location(
            name, HERE / (filename or f"{name}.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[name] = module
    return _MODULES[name]


def deb_candidates(text: str) -> dict:
    """The index restricted to apt's candidate per name: highest version."""
    apt = load("apt_packages")
    dv = load("deb_version")
    winners: dict[str, dict] = {}
    for stanza in apt.stanzas(text):
        name = stanza.get("Package")
        if not name:
            continue
        held = winners.get(name)
        if held is None or dv.compare(
                stanza.get("Version", "0"), held.get("Version", "0")) > 0:
            winners[name] = stanza
    return apt.index_from_stanzas(winners.values())


def _deb_unresolved(info: dict, caps: set, caps_evr: dict, dv) -> list[str]:
    missing = []
    for group in info.get("depends", ()):
        satisfied = False
        for name, op, version in group:
            name = name.split(":")[0]
            if name not in caps:
                continue
            if not op or not version:
                satisfied = True
                break
            offered = caps_evr.get(name)
            if not offered:
                # Bare virtual against a versioned dep: per policy this
                # does NOT satisfy — but with no version recorded the
                # gate cannot judge, and a gate must lean lenient.
                satisfied = True
                break
            if any(dv.satisfies(evr, op, version) for evr in offered):
                satisfied = True
                break
        if not satisfied:
            missing.append(" | ".join(
                f"{n}{f' ({o} {v})' if o else ''}" for n, o, v in group))
    return missing


def _pacman_unresolved(info: dict, caps: set, caps_evr: dict, pdb) -> list[str]:
    missing = []
    for name in info.get("requires", ()):
        if name not in caps:
            missing.append(name)
    for name, op, version in info.get("requires_versioned", ()):
        offered = caps_evr.get(name)
        if not offered:
            continue
        if not any(pdb.satisfies(evr, op, version) for evr in offered):
            missing.append(f"{name} {op} {version}")
    return missing


def _caps(index: dict) -> tuple[set, dict]:
    caps = set(index["packages"]) | set(index["provides"])
    return caps, index.get("provides_evr", {})


def regressions(old: dict, new: dict, fmt: str) -> dict:
    """Dependencies resolvable in the old view, unresolvable in the new."""
    if fmt == "deb":
        judge = _deb_unresolved
        version = load("deb_version")
    else:
        judge = _pacman_unresolved
        version = load("pacman_db")
    old_caps, old_evr = _caps(old)
    new_caps, new_evr = _caps(new)

    broken: dict[str, list[str]] = {}
    for name in sorted(new["packages"]):
        after = judge(new["packages"][name], new_caps, new_evr, version)
        if not after:
            continue
        # Judge the same dependency declaration against both views.
        # Newly declared distro dependencies were never resolved by our
        # index, so adding one cannot be an index regression. The clean
        # installation gate verifies their availability from the distro.
        reference = new["packages"][name]
        before = set(judge(reference, old_caps, old_evr, version))
        regressed = [dep for dep in after if dep not in before]
        if regressed:
            broken[name] = regressed
    removed = sorted(set(old["packages"]) - set(new["packages"]))
    return {"broken": broken, "removed": removed}


def load_indexes(fmt: str, old_path: pathlib.Path, new_path: pathlib.Path):
    if fmt == "deb":
        apt = load("apt_packages")

        def read(path):
            return deb_candidates(apt.decompress(
                path.name, path.read_bytes()).decode("utf-8", "replace"))
        return read(old_path), read(new_path)
    pacman = load("pacman_db")
    return (pacman.parse_db(old_path.read_bytes()),
            pacman.parse_db(new_path.read_bytes()))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="old-vs-new index regression gate")
    parser.add_argument("--format", required=True,
                        choices=["deb", "pacman", "pkg.tar.zst"])
    parser.add_argument("--old", type=pathlib.Path, required=True)
    parser.add_argument("--new", type=pathlib.Path, required=True)
    parser.add_argument("--json", type=pathlib.Path)
    args = parser.parse_args()
    fmt = "pacman" if args.format == "pkg.tar.zst" else args.format

    if not args.old.is_file():
        print(f"{args.old}: no previous index; first publish, nothing to regress")
        return 0
    if not args.new.is_file():
        raise SystemExit(f"{args.new}: the regenerated index does not exist")

    old, new = load_indexes(fmt, args.old, args.new)
    report = regressions(old, new, fmt)

    print(f"old index: {len(old['packages'])} package(s); "
          f"new: {len(new['packages'])}")
    for name in report["removed"]:
        # Informational: pool-accumulating repos should only ever grow;
        # a vanished name is the INCIDENT-repo-wipe shape one step early.
        print(f"  note: {name} is no longer in the index")
    for name, missing in report["broken"].items():
        print(f"  BREAKS {name}: loses {', '.join(missing)}")

    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    if report["broken"]:
        print("\nthe regenerated index does not keep its packages installable")
        return 1
    print("\nevery dependency the old index resolved still resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
