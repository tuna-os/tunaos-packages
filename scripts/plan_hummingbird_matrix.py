#!/usr/bin/env python3
"""Fan `build-hummingbird-desktops.yml` out over desktops.

The workflow used to be a single job with a `desktop:` choice, so the whole
670-package gap was driven one dispatch at a time on one runner.  Measured on
five real runs (see docs/hummingbird-throughput.md) the build step is 95.6%-
97.8% mock time, so a job is busy essentially all of its wall clock -- the
ceiling is how many machines the work is spread over, and there was one.

This emits the matrix that spreads it.  It is deliberately the only place that
knows what a "desktop" is: the list is read out of the manifest's tier names,
so adding a sixth desktop to build-order-hummingbird-desktops.yml fans out to
six jobs with no workflow edit.

Selection rules, matching what the workflow's own `Select tiers` step then
does inside each job:

  explicit `tiers:`   one job.  The tier list is absolute -- it names the
                      tiers to build, across whatever desktops they belong
                      to -- so splitting it per desktop would change which
                      tiers run, not just where.
  `desktop: all`      one job per desktop, in manifest order.
  `desktop: <name>`   one job.

`bootstrap-*` is not a desktop.  Those tiers are the PEP-517 backends #268
added, and the workflow prepends them to every selection; they are ~10 small
packages and every desktop job builds them, which costs one 10-minute prefix
per job in parallel rather than a cross-job dependency and an artifact hop.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

BOOTSTRAP_PREFIX = "bootstrap-"


def desktops_in(manifest: dict) -> list[str]:
    """Desktop names in manifest order, deduplicated.

    A tier is named `<desktop>-<NN>`, so the desktop is everything before the
    last dash.  Reading them out of the manifest rather than hard-coding them
    keeps this honest when the gap is re-measured.
    """
    seen: list[str] = []
    for tier in manifest["tiers"]:
        name = tier["name"]
        if name.startswith(BOOTSTRAP_PREFIX):
            continue
        desktop = name.rsplit("-", 1)[0]
        if desktop not in seen:
            seen.append(desktop)
    return seen


def plan(manifest: dict, desktop: str, tiers: str) -> list[str]:
    known = desktops_in(manifest)
    if tiers.strip():
        return [desktop]
    if desktop == "all":
        return known
    if desktop not in known:
        raise SystemExit(
            f"no tiers match desktop={desktop!r}; the manifest has {known}"
        )
    return [desktop]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--desktop", required=True)
    parser.add_argument("--tiers", default="")
    args = parser.parse_args(argv)

    manifest = yaml.safe_load(args.manifest.read_text())
    print("desktops=" + json.dumps(plan(manifest, args.desktop, args.tiers)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
