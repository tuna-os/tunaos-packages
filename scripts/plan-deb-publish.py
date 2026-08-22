#!/usr/bin/env python3
"""Emit the deb publish matrix from the target contract and the recipes.

publish-tideforge-debs.yml was the last publisher with a hand-written
`include:` list. publish-tideforge-rpms.yml resolves its matrix from the
recipes in a plan job and publish-build-chain-rpms.yml calls
plan-build-chain-publish.py; only the deb one asked a human to keep a literal
matrix in step with reality (#479).

That is not a style complaint. A hand-listed matrix cannot report a MISSING
row, because a row that was never written looks exactly like a row nobody
wanted -- which is how every deb ever published turned out to be amd64: the
matrix had no `arch` key at all, and nothing anywhere said it should. The
same list also had to be widened by hand whenever a recipe proved out.

What is derived here rather than typed:

  * the architectures come from the target's `architectures` in
    manifests/package-factory.yaml -- the same list the gate builds, so the
    two cannot drift apart again;
  * the build image comes from the target's `probe_image`, exactly as
    plan-package-factory.py resolves a cell's image;
  * every requested package is checked to EXIST and to declare the target it
    is being published to, so a typo or an untargeted recipe fails at plan
    time instead of part-way through a wave that is already writing to R2.

What is deliberately NOT derived: the package list. Publishing stays an
explicit act with an explicit default wave -- "nothing gets published that a
gate cell has not exercised" is a judgement a human makes, and turning it
into "every recipe that targets deb" would silently widen what ships.

The distro LABELS are also not derived. `ubuntu` and `debian-sid` are live
R2 prefixes (r2:bluefin/tideforge/<distro>/, served at
repo.tunaos.org/tideforge/<distro>/); renaming one would orphan a published
repository, so the label->target mapping is explicit and pinned by a test.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import factory_contract  # noqa: E402  (needs the path above)

ROOT = pathlib.Path(__file__).resolve().parents[1]
FACTORY = ROOT / "manifests" / "package-factory.yaml"

# label -> target. The label is the published repository's path segment and
# cannot change without orphaning the repo; the target is what the renderer
# and the contract call the same distribution.
DISTRO_TARGETS = {
    "ubuntu": "ubuntu",
    "debian-sid": "debian",
}

# Native runners. A cross-built deb is not what these targets install, so
# each arch builds on its own hardware rather than under qemu.
RUNNERS = {
    "amd64": "ubuntu-latest",
    "arm64": "ubuntu-24.04-arm",
}


def fail(message: str) -> None:
    sys.exit(f"plan-deb-publish: {message}")


def split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def plan(packages: list[str], distros: list[str], arches: list[str] | None) -> list[dict]:
    targets = (yaml.safe_load(FACTORY.read_text(encoding="utf-8")) or {}).get("targets") or {}
    if not packages:
        fail("no packages requested")

    unknown = sorted(set(distros) - set(DISTRO_TARGETS))
    if unknown:
        fail(f"unknown distro(s): {unknown}; known: {sorted(DISTRO_TARGETS)}")

    include: list[dict] = []
    for distro in distros:
        target_name = DISTRO_TARGETS[distro]
        target = targets.get(target_name)
        if not target:
            fail(f"{distro} maps to target {target_name}, which the contract does not declare")
        if target.get("format") != "deb":
            fail(f"target {target_name} is format {target.get('format')!r}, not deb")
        image = target.get("probe_image")
        if not image:
            fail(f"target {target_name} declares no probe_image to build in")

        declared = list(target.get("architectures") or [])
        if not declared:
            fail(f"target {target_name} declares no architectures")
        selected = arches or declared
        missing = sorted(set(selected) - set(declared))
        if missing:
            fail(f"{target_name} does not declare arch(es) {missing}; it declares {declared}")
        unrunnable = sorted(set(selected) - set(RUNNERS))
        if unrunnable:
            fail(f"no runner for arch(es) {unrunnable}")

        for package in packages:
            recipe_path = ROOT / "packages" / package / "package.yaml"
            if not recipe_path.is_file():
                fail(f"no recipe at packages/{package}/package.yaml")
            recipe = yaml.safe_load(recipe_path.read_text(encoding="utf-8")) or {}
            if target_name not in (recipe.get("targets") or []):
                fail(f"{package} does not target {target_name}; refusing to publish it there")
            for arch in selected:
                include.append({
                    "package": package,
                    "distro": distro,
                    "target": target_name,
                    "image": image,
                    "arch": arch,
                    "runner": RUNNERS[arch],
                    "cell_id": factory_contract.tideforge_cell_id(
                        package, target_name, arch
                    ),
                })

    if not include:
        fail("empty publish matrix")
    return include


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", required=True, help="comma-separated recipe names under packages/")
    parser.add_argument("--distros", default="ubuntu,debian-sid", help="comma-separated distro labels")
    parser.add_argument("--arches", default="", help="comma-separated arches; default is every arch the target declares")
    parser.add_argument("--github-output", help="append matrix=<json> here")
    args = parser.parse_args()

    include = plan(split(args.packages), split(args.distros), split(args.arches) or None)
    matrix = json.dumps({"include": include})
    distros = sorted({row["distro"] for row in include})
    arches = sorted({row["arch"] for row in include})

    # The publish and verify jobs used to hard-code `distro: [ubuntu,
    # debian-sid]` and `arch: [amd64, arm64]` of their own. That is wrong the
    # moment a wave is narrowed: publishing a distro the build never produced
    # rclone-syncs an EMPTY tree over a live repository -- the #124
    # repo-wipe shape -- and verifying an unpublished arch fails a wave that
    # did exactly what it was asked to. Both now follow what was actually
    # built.
    verify_rows = []
    for row in include:
        key = (row["distro"], row["arch"])
        if key not in {(r["distro"], r["arch"]) for r in verify_rows}:
            verify_rows.append({
                "distro": row["distro"],
                "arch": row["arch"],
                "image": row["image"],
                "runner": row["runner"],
            })
    verify_matrix = json.dumps({"include": verify_rows})

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"matrix={matrix}\n")
            handle.write(f"verify_matrix={verify_matrix}\n")
            handle.write(f"distros={json.dumps(distros)}\n")
            handle.write(f"arches={json.dumps(arches)}\n")
    print(json.dumps({
        "count": len(include),
        "distros": distros,
        "arches": arches,
        "verify_count": len(verify_rows),
        "matrix": json.loads(matrix),
        "verify_matrix": json.loads(verify_matrix),
    }, indent=2))


if __name__ == "__main__":
    main()
