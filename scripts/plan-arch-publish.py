#!/usr/bin/env python3
"""Emit the Arch publish matrices from the contract and the recipes.

Arch is the last format with no publisher at all (#479). 23 recipes target it
-- the whole DankMaterialShell stack, niri, greetd, bazaar, libseat and more --
and every one is planned, built, verified, smoke-tested and then unreachable:
docs/FACTORY-STATUS.md reports the target as "no published_index declared".

`status` does not gate publishing (hummingbird is `scaffold` and publishes to
a live index), and the contract already declares `r2_path: pacman/arch/{arch}`,
so this is a half-wired path rather than a decision.

Deliberately a separate planner from plan-rpm-publish.py rather than a `format`
branch inside it: pacman needs repo-add and a `.db` rather than createrepo_c
and repodata, its repository layout is flat, and its verify has an ordering
hazard no rpm repository has (see publish-tideforge-arch.yml). Sharing a
planner would mean one script whose every branch is format-specific.
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
SERVED_ROOT = "https://repo.tunaos.org/"

# The pacman repository's name. It is also the db filename pacman requests
# (`<name>.db`), so changing it renames files consumers have configured.
REPO_NAME = "tunaos"

RUNNERS = {"x86_64": "ubuntu-latest", "aarch64": "ubuntu-24.04-arm"}


def fail(message: str) -> None:
    sys.exit(f"plan-arch-publish: {message}")


def split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def plan(packages: list[str], arches: list[str] | None) -> dict:
    targets = (yaml.safe_load(FACTORY.read_text(encoding="utf-8")) or {}).get("targets") or {}
    target = targets.get("arch")
    if not target:
        fail("the contract declares no arch target")
    if target.get("format") != "pkg.tar.zst":
        fail(f"arch is format {target.get('format')!r}, not pkg.tar.zst")
    image = target.get("probe_image")
    if not image:
        fail("arch declares no probe_image to build in")
    r2_path = target.get("r2_path")
    if not r2_path:
        fail("arch declares no r2_path to publish into")
    if not packages:
        fail("no packages requested")

    declared = list(target.get("architectures") or [])
    selected = arches or declared
    missing = sorted(set(selected) - set(declared))
    if missing:
        fail(f"arch does not declare arch(es) {missing}; it declares {declared}")
    unrunnable = sorted(set(selected) - set(RUNNERS))
    if unrunnable:
        fail(f"no runner for arch(es) {unrunnable}")

    build = []
    for package in packages:
        recipe = ROOT / "packages" / package / "package.yaml"
        if not recipe.is_file():
            fail(f"no recipe at packages/{package}/package.yaml")
        data = yaml.safe_load(recipe.read_text(encoding="utf-8")) or {}
        if "arch" not in (data.get("targets") or []):
            fail(f"{package} does not target arch; refusing to publish it there")
        for arch in selected:
            build.append({
                "package": package,
                "arch": arch,
                "runner": RUNNERS[arch],
                "image": image,
                "cell_id": factory_contract.tideforge_cell_id(package, "arch", arch),
            })

    publish = []
    for arch in selected:
        src = r2_path.replace("{arch}", arch)
        publish.append({
            "arch": arch,
            "src": src,
            "served": f"{SERVED_ROOT}{src}/",
            "runner": RUNNERS[arch],
            "repo_name": REPO_NAME,
        })

    return {
        "image": image,
        "repo_name": REPO_NAME,
        "arches": selected,
        "build": {"include": build},
        "publish": {"include": publish},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", required=True)
    parser.add_argument("--arches", default="")
    parser.add_argument("--github-output")
    args = parser.parse_args()

    result = plan(split(args.packages), split(args.arches) or None)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"matrix={json.dumps(result['build'])}\n")
            handle.write(f"publish_matrix={json.dumps(result['publish'])}\n")
            handle.write(f"arches={json.dumps(result['arches'])}\n")
            handle.write(f"image={result['image']}\n")
            handle.write(f"repo_name={result['repo_name']}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
