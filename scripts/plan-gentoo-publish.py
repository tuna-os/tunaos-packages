#!/usr/bin/env python3
"""Emit the Gentoo publish matrices from the contract and the recipes.

The gpkg counterpart of plan-arch-publish.py (ADR 0002). Two things are
published for the `gentoo` target:

- the binhost: signed gpkgs plus Portage's `Packages` index at the target's
  r2_path (gentoo/binpkgs/<arch>), what binrepos.conf points at;
- the overlay: every recipe that targets gentoo, rendered to ebuilds with
  their Manifests, as one zip at gentoo/overlay/ for repos.conf's
  `sync-type = zipfile`. It is rendered from ALL such recipes, not just the
  wave, because the zip replaces the whole repository on every sync.

A separate planner rather than a branch of another one for the reason the
Arch planner gives: the index, the signing and the layout are all
format-specific.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import factory_contract  # noqa: E402  (needs the path above)
import publisher_contract  # noqa: E402
import tideforge  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVED_ROOT = "https://repo.tunaos.org/"
TARGET = "gentoo"
OVERLAY_SRC = "gentoo/overlay"
OVERLAY_ZIP = f"{tideforge.GENTOO_REPO_NAME}-overlay.zip"

RUNNERS = {"amd64": "ubuntu-latest"}


def fail(message: str) -> None:
    sys.exit(f"plan-gentoo-publish: {message}")


def overlay_packages() -> list[str]:
    names = []
    for path in sorted((ROOT / "packages").glob("*/package.yaml")):
        if path.parent.name.startswith("_"):
            continue
        recipe = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if TARGET in (recipe.get("targets") or []):
            names.append(path.parent.name)
    return names


def plan(packages: list[str], arches: list[str] | None) -> dict:
    target = publisher_contract.target(TARGET, "gpkg", fail)
    image = target.get("probe_image")
    r2_path = target.get("r2_path")
    if not r2_path:
        fail(f"{TARGET} declares no r2_path to publish into")
    if not packages:
        fail("no packages requested")

    selected = publisher_contract.architectures(TARGET, target, arches, RUNNERS, fail)

    build = []
    for package in packages:
        recipe = publisher_contract.recipe(package, TARGET, fail)
        for arch in selected:
            build.append({
                "package": package,
                "arch": arch,
                "runner": RUNNERS[arch],
                "image": image,
                "category": recipe["outputs"]["gpkg"]["category"],
                "atom": f"{recipe['outputs']['gpkg']['category']}/{recipe['name']}",
                "cell_id": factory_contract.tideforge_cell_id(package, TARGET, arch),
            })

    publish = []
    for arch in selected:
        src = r2_path.replace("{arch}", arch)
        publish.append({
            "arch": arch,
            "src": src,
            "served": f"{SERVED_ROOT}{src}/",
            "runner": RUNNERS[arch],
        })

    return {
        "image": image,
        "arches": selected,
        "overlay_packages": overlay_packages(),
        "overlay_src": OVERLAY_SRC,
        "overlay_url": f"{SERVED_ROOT}{OVERLAY_SRC}/{OVERLAY_ZIP}",
        "overlay_zip": OVERLAY_ZIP,
        "build": {"include": build},
        "publish": {"include": publish},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", required=True)
    parser.add_argument("--arches", default="")
    parser.add_argument("--github-output")
    args = parser.parse_args()

    arches = publisher_contract.split(args.arches) or None
    result = plan(publisher_contract.split(args.packages), arches)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"matrix={json.dumps(result['build'])}\n")
            handle.write(f"publish_matrix={json.dumps(result['publish'])}\n")
            handle.write(f"image={result['image']}\n")
            handle.write(f"overlay_packages={','.join(result['overlay_packages'])}\n")
            handle.write(f"overlay_src={result['overlay_src']}\n")
            handle.write(f"overlay_url={result['overlay_url']}\n")
            handle.write(f"overlay_zip={result['overlay_zip']}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
