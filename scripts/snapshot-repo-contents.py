#!/usr/bin/env python3
"""Snapshot what repo.tunaos.org actually serves, one JSON for the site.

## Why this exists

docs/factory-status.json answers "how much of the plan has been built". It
does NOT answer "what is in the repository" -- and that is the question
someone actually has when they are deciding whether to point a machine at
repo.tunaos.org. Until now the only way to answer it was to add the repo to
a box and ask dnf, which means you have to already trust it to find out
what it contains.

The served indexes are public, so nothing here is privileged: this reads
exactly what any client's package manager reads at the exact same URLs the
factory contract declares in `published_index`, through the same
format-neutral reader (scripts/repo_index.py) the hygiene checks use. The
consequence worth stating: the browser cannot show a package the repo does
not serve, and cannot hide one it does, because it is not a separate list
that someone has to remember to update. It is the index.

## Failure is data, not an exception

An index that 404s, times out, or parses as garbage is recorded as an entry
carrying its `error` and no packages -- it is NOT dropped, and it does not
abort the run. A target whose index is unreachable is the single most
important thing this file can report (that is what #519 looked like from
outside: names silently absent), so a snapshot that omitted it in the name
of a clean exit code would hide the one failure it exists to catch.

Usage:
    snapshot-repo-contents.py --out docs/repo-contents.json
    snapshot-repo-contents.py --target el10 --out -   # one target, stdout
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import repo_index  # noqa: E402
import yaml  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "manifests" / "package-factory.yaml"


def indexes(contract: dict, only: str | None) -> list[dict]:
    """Every (target, arch, url) the contract says is served.

    One arch can declare more than one index -- el10 x86_64 has two, because
    two publishers write to disjoint prefixes (#467) -- so this is a list of
    index rows, not a dict keyed by arch.
    """
    rows: dict[tuple[str, str], dict] = {}
    for target, spec in sorted(contract.get("targets", {}).items()):
        if only and target != only:
            continue
        published = spec.get("published_index") or {}
        if isinstance(published, str):          # a bare string, no arch split
            published = {"": published}
        for arch, urls in sorted(published.items()):
            if isinstance(urls, str):
                urls = [urls]
            for url in urls:
                if not url or not str(url).strip():
                    continue
                url = str(url).strip()
                # Keyed by (target, url), NOT by (target, arch, url). A flat
                # deb repo is ONE index that both amd64 and arm64 point at --
                # reading it once per arch would download it twice and report
                # its 16 packages as 32. The arches that name it are recorded
                # on the row instead, which is also the truthful shape: the
                # index is not per-arch, the pointer to it is.
                key = (target, url)
                if key in rows:
                    if arch and arch not in rows[key]["arches"]:
                        rows[key]["arches"].append(arch)
                    continue
                rows[key] = {
                    "target": target,
                    "arches": [arch] if arch else [],
                    "format": spec.get("format", "rpm"),
                    "url": url,
                }
    return list(rows.values())


def read(row: dict, cache: pathlib.Path) -> dict:
    """One index's packages, or the reason there are none."""
    out = dict(row, packages=[], error=None)
    try:
        # NOT repo_name=target. For pkg.tar.zst the db file is named after
        # the REPOSITORY (scripts/plan-arch-publish.py: REPO_NAME = "tunaos",
        # published as tunaos.db), not after the factory's target id, so
        # passing "arch" here would fetch arch.db, 404, and report the index
        # as unreachable the day it is first declared. Omitting it takes the
        # same default the hygiene checks take.
        rows = list(repo_index.iter_rows(row["url"], row["format"], cache))
    except Exception as exc:                    # noqa: BLE001 -- see docstring
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    for entry in rows:
        # Source packages are build inputs, not something anyone installs.
        # They stay out of the browser for the same reason publish-rpm-wave.sh
        # excludes them from the served tree.
        if entry.get("arch") == "src":
            continue
        out["packages"].append({
            "name": entry["name"],
            "arch": entry.get("arch") or "",
            "evr": entry.get("evr") or "",
            "source": repo_index.source_name(row["format"], entry.get("srpm")),
            "location": entry.get("location"),
        })
    out["packages"].sort(key=lambda p: (p["name"], p["arch"], p["evr"]))
    return out


def snapshot(contract: dict, only: str | None, cache: pathlib.Path,
             now: str) -> dict:
    read_rows = [read(row, cache) for row in indexes(contract, only)]
    return {
        "generated": now,
        "indexes": read_rows,
        "totals": {
            "indexes": len(read_rows),
            "unreachable": sum(1 for r in read_rows if r["error"]),
            "packages": sum(len(r["packages"]) for r in read_rows),
            "names": len({p["name"] for r in read_rows
                          for p in r["packages"]}),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="docs/repo-contents.json",
                    help="output path, or - for stdout")
    ap.add_argument("--contract", default=str(CONTRACT))
    ap.add_argument("--target", default=None,
                    help="limit to one target (default: every declared one)")
    ap.add_argument("--cache", default=None,
                    help="index download cache (default: a temp dir)")
    ap.add_argument("--now", default=None,
                    help="override the generated timestamp (tests)")
    args = ap.parse_args(argv)

    contract = yaml.safe_load(
        pathlib.Path(args.contract).read_text(encoding="utf-8"))
    now = args.now or dt.datetime.now(dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    with tempfile.TemporaryDirectory() as tmp:
        cache = pathlib.Path(args.cache or tmp)
        cache.mkdir(parents=True, exist_ok=True)
        data = snapshot(contract, args.target, cache, now)

    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
    else:
        pathlib.Path(args.out).write_text(text, encoding="utf-8")

    unreachable = [r for r in data["indexes"] if r["error"]]
    for row in unreachable:
        print(f"::warning::{row['target']} {'/'.join(row['arches'])} "
              f"{row['url']}: {row['error']}", file=sys.stderr)
    print(f"{data['totals']['packages']} packages / "
          f"{data['totals']['names']} names across "
          f"{data['totals']['indexes']} indexes "
          f"({len(unreachable)} unreachable)", file=sys.stderr)
    # Deliberately 0 even with unreachable indexes: the snapshot SUCCEEDED at
    # recording that they are unreachable, and the page says so. Failing here
    # would take the whole site down over one dead prefix.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
