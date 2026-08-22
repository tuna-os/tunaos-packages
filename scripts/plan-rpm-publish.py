#!/usr/bin/env python3
"""Emit the RPM publish matrices for a target, from the contract and recipes.

publish-tideforge-rpms.yml hard-coded el10 in six places: the plan job's
`"el10" not in targets` check, CELL_ID, TARGET, IMAGE, a `case $arch` for the
R2 sync path, and a second `case $arch` for the served verify URL. That is why
openSUSE Tumbleweed — a target whose contract declares
`r2_path: rpm/opensuse-tumbleweed/{arch}` — has no publisher at all, and its
packages are gated, verified, smoke-tested and unreachable (#479).

`status` does not gate this: hummingbird is `scaffold` and publishes to a live
index. The two targets without a publisher (opensuse-tumbleweed, arch) both
declare an r2_path, so they are half-wired paths rather than decisions.

## el10's paths are bespoke and are NOT derived

el10 x86_64 does not publish to its own contract r2_path. It syncs into
`repo/10-stream-x86_64` and mirrors to `repo/10-x86_64`, because that prefix is
the pre-existing COPR-mirror repo with hundreds of packages that this factory
merges into; `rpm/el10/x86_64` 404s. aarch64 has no such legacy and uses the
contract path. Deriving el10 from the contract would silently redirect a live
repository, so its mapping is stated explicitly below and pinned by a test
asserting the emitted values are byte-identical to what the workflow used to
hard-code. Every OTHER target uses its contract r2_path.

The `min_rpms` guard is the #124 / INCIDENT-repo-wipe-gnome lesson: `rclone
sync` makes the destination match the source, so a partial sync-down would
DELETE the served repo. A prefix known to hold hundreds of packages refuses to
proceed from a suspiciously small download; a prefix this workflow creates
starts empty by design and cannot use that guard.
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

RUNNERS = {"x86_64": "ubuntu-latest", "aarch64": "ubuntu-24.04-arm"}

# target -> arch -> overrides. Only el10 has any; see the module docstring.
BESPOKE = {
    "el10": {
        "x86_64": {
            "src": "repo/10-stream-x86_64",
            "mirror": "repo/10-x86_64",
            "served": "https://repo.tunaos.org/repo/10/x86_64/",
            "min_rpms": 100,
        },
        "aarch64": {
            "src": "rpm/el10/aarch64",
            "served": "https://repo.tunaos.org/rpm/el10/aarch64/",
        },
    },
}


def fail(message: str) -> None:
    sys.exit(f"plan-rpm-publish: {message}")


def split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def plan(target_name: str, packages: list[str], arches: list[str] | None) -> dict:
    targets = (yaml.safe_load(FACTORY.read_text(encoding="utf-8")) or {}).get("targets") or {}
    target = targets.get(target_name)
    if not target:
        fail(f"unknown target {target_name!r}; the contract declares {sorted(targets)}")
    if target.get("format") != "rpm":
        fail(f"target {target_name} is format {target.get('format')!r}, not rpm")
    image = target.get("probe_image")
    if not image:
        fail(f"target {target_name} declares no probe_image to build in")
    if not packages:
        fail("no packages requested")

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

    r2_path = target.get("r2_path")
    if not r2_path:
        fail(f"target {target_name} declares no r2_path to publish into")

    build = []
    for package in packages:
        recipe = ROOT / "packages" / package / "package.yaml"
        if not recipe.is_file():
            fail(f"no recipe at packages/{package}/package.yaml")
        data = yaml.safe_load(recipe.read_text(encoding="utf-8")) or {}
        if target_name not in (data.get("targets") or []):
            fail(f"{package} does not target {target_name}; refusing to publish it there")
        for arch in selected:
            build.append({
                "package": package,
                "arch": arch,
                "runner": RUNNERS[arch],
                "target": target_name,
                "image": image,
                "cell_id": factory_contract.tideforge_cell_id(package, target_name, arch),
            })

    publish = []
    for arch in selected:
        override = BESPOKE.get(target_name, {}).get(arch, {})
        src = override.get("src") or r2_path.replace("{arch}", arch)
        row = {
            "arch": arch,
            "src": src,
            "mirror": override.get("mirror", ""),
            "served": override.get("served") or f"{SERVED_ROOT}{src}/",
            "min_rpms": override.get("min_rpms", 0),
            "runner": RUNNERS[arch],
        }
        publish.append(row)

    return {
        "target": target_name,
        "image": image,
        "arches": selected,
        "build": {"include": build},
        "publish": {"include": publish},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="el10")
    parser.add_argument("--packages", required=True)
    parser.add_argument("--arches", default="")
    parser.add_argument("--github-output")
    args = parser.parse_args()

    result = plan(args.target, split(args.packages), split(args.arches) or None)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"matrix={json.dumps(result['build'])}\n")
            handle.write(f"publish_matrix={json.dumps(result['publish'])}\n")
            handle.write(f"arches={json.dumps(result['arches'])}\n")
            handle.write(f"image={result['image']}\n")
            handle.write(f"target={result['target']}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
