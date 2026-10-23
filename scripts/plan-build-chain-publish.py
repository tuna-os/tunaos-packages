#!/usr/bin/env python3
"""Resolve build-chain cells for publishing, and refuse the unsafe ones.

The build-chain families each declare an `r2_path` in the catalog, so where a
cell publishes is data rather than something this workflow invents. What the
data does NOT settle is whether that destination is safe to sync into, and
two of the answers are "no".

DESTINATION COLLISION. publish-tideforge-rpms.yml syncs x86_64 to
`repo/10-stream-x86_64` and then mirrors the same tree to `repo/10-x86_64`:

    rclone sync repo/ "r2:${R2_BUCKET}/repo/10-x86_64/"

`gnome50-el10-x86_64` declared exactly that path until 2026-09-03 (it now
owns gnome50/10-stream-x86_64, like every other family). `rclone sync` makes
the destination match the source, so publishing a family there means the
next tideforge publish deletes every one of its packages, and the family's
publish deletes every tideforge package in between. Serialising the two
workflows (the shared concurrency group) stops them racing; it does not stop
them overwriting.

That is a catalog question -- the family wants its own prefix, or the mirror
wants to be a union rather than a copy -- and it is not one to guess at
while holding credentials that can empty a live repo. So it is refused here
by name, loudly, with the reason. Run 33751204743 is what that looks like:
gnome50 refused at plan, nothing touched.

NO DESTINATION. A cell with an empty `r2_path`, or `publish: false`, is
declaring it has nowhere to go. `fprintd-el10-x86_64` is both.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from factory import publisher_contract

ROOT = Path(__file__).resolve().parents[1]

# Prefixes publish-tideforge-rpms.yml syncs into. Publishing a build-chain
# wave to any of them means the two publishers delete each other's packages.
TIDEFORGE_DESTINATIONS = {
    "repo/10-stream-x86_64",
    "repo/10-x86_64",
    "rpm/el10/aarch64",
}


def build_chain_cells():
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "plan-package-factory.py"),
         "--selector", "engine=build-chain"],
        capture_output=True, text=True, check=True,
    ).stdout
    cells = []
    for matrix in json.loads(out)["matrices"]:
        cells.extend(json.loads(matrix)["include"])
    return {c["id"]: c for c in cells}


def resolve(requested, available):
    """Return (selected, rejections). Never raises on a bad cell -- the caller
    reports every reason at once rather than one per re-run."""
    selected, rejections = [], []
    for name in requested:
        cell = available.get(name)
        if cell is None:
            rejections.append(
                f"{name}: not a build-chain cell "
                f"(known: {', '.join(sorted(available))})"
            )
            continue
        if not cell.get("source_paths"):
            rejections.append(
                f"{name}: declares no source_paths, so SOURCE_DATE_EPOCH "
                "cannot be derived from them; refusing rather than falling "
                "back to the repository's last commit, which would re-key "
                "this cell on EVERY commit"
            )
            continue
        rejection = publisher_contract.production_rejection(
            cell.get("target", ""), cell.get("r2_path") or "")
        if rejection:
            rejections.append(f"{name}: {rejection}")
            continue
        if cell.get("publish") is False:
            rejections.append(f"{name}: catalog sets publish: false")
            continue
        path = (cell.get("r2_path") or "").strip().strip("/")
        if not path:
            rejections.append(f"{name}: no r2_path in the catalog")
            continue
        if path in TIDEFORGE_DESTINATIONS:
            rejections.append(
                f"{name}: r2_path '{path}' is also written by "
                "publish-tideforge-rpms.yml, which syncs (not copies) into it "
                "-- publishing here would delete that publisher's packages, "
                "and its next run would delete these. Give the family its own "
                "prefix, or make the mirror a union, before publishing it."
            )
            continue
        selected.append({
            "id": cell["id"],
            "target": cell.get("target", ""),
            "architecture": cell.get("architecture", ""),
            "runner": cell.get("runner", "ubuntu-latest"),
            "image": cell.get("image", ""),
            "manifest": cell.get("manifest", ""),
            "mock_config": cell.get("mock_config", ""),
            # Needed to date the build reproducibly: SOURCE_DATE_EPOCH is the
            # newest commit touching the manifest or any source path, the same
            # derivation package-factory-cell.yml's identity step uses.
            "source_paths": cell.get("source_paths", []),
            # The four remaining native-key inputs. The publish build job now
            # computes the SAME action key as the nightly so it can cache-hit
            # a chain the nightly completed and resume a partial it deferred;
            # omitting these made the key differ (empty vs "stable") and the
            # cache could never bridge the two workflows.
            "dependency_tree": cell.get("dependency_tree", ""),
            "target_queue": cell.get("target_queue", ""),
            "track": cell.get("track", ""),
            "series": cell.get("series", ""),
            "r2_path": path,
        })
    return selected, rejections


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cells", required=True)
    ap.add_argument("--github-output")
    args = ap.parse_args(argv)

    requested = [c.strip() for c in args.cells.split(",") if c.strip()]
    if not requested:
        print("ERROR: no cells requested -- the 'cells' workflow_dispatch "
              "input is empty (it has no default: pass at least one "
              "build-chain cell id, e.g. hummingbird-x86_64)", file=sys.stderr)
        return 2

    selected, rejections = resolve(requested, build_chain_cells())

    for reason in rejections:
        print(f"::error::{reason}")
    for cell in selected:
        print(f"  {cell['id']} -> {cell['r2_path']}")

    # Any rejection fails the run. A partial publish that silently dropped
    # the cell someone asked for is worse than no publish: the wave looks
    # green and the package is still missing.
    if rejections:
        print(f"ERROR: {len(rejections)} cell(s) refused", file=sys.stderr)
        return 1
    if not selected:
        print("ERROR: nothing to publish", file=sys.stderr)
        return 1

    matrix = json.dumps({"include": selected})
    if args.github_output:
        with open(args.github_output, "a") as fh:
            fh.write(f"matrix={matrix}\n")
    else:
        print(matrix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
