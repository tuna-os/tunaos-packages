#!/usr/bin/env python3
"""Stage explicit EL10 candidate inputs; never publish or change production pins."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

import yaml

CELLS = {"skipjack": "el10", "albacore": "alma10", "yellowfin": "alma10-kitten"}
# Candidate-subset platform map (no consumer adapter consulted): mirrors the
# per-arch platform spelling used by consumer contracts (PR819).
ARCH_PLATFORMS = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}
# The EL10-only subset: skipjack cells alone. Alma consumer cells are absent
# here and must never be relabeled as EL10 evidence.
EL10_ONLY_CELLS = frozenset(
    {("skipjack", "el10", arch) for arch in ("x86_64", "aarch64")}
)
BUILD = [
    "clang-devel",
    "pkgconfig(dbus-1)",
    "pkgconfig(glib-2.0)",
    "pkgconfig(gtk4) >= 4.12",
    "pkgconfig(gtk4-layer-shell-0) >= 1.1",
    "pkgconfig(libadwaita-1)",
    "pkgconfig(libdrm)",
    "pkgconfig(libinput)",
    "pkgconfig(libsecret-1)",
    "pkgconfig(xkbcommon)",
    "pkgconfig(xkbcommon-x11)",
    "pkgconfig(gbm)",
    "pkgconfig(libpipewire-0.3)",
    "pkgconfig(libseat)",
    "pkgconfig(libsystemd)",
    "pkgconfig(libudev)",
    "pkgconfig(egl)",
    "pkgconfig(wayland-client)",
    "pkgconfig(wayland-server)",
    "wayland-protocols-devel",
]
RUNTIME = [
    "dbus",
    "dconf",
    "pam",
    "gsettings-desktop-schemas",
    "libcanberra-gtk3",
    "libcanberra-pulseaudio",
    "sound-theme-freedesktop",
    "xdg-desktop-portal",
    "xdg-desktop-portal-gtk",
]
PORTAL_DESTINATIONS = {
    "share/xdg-desktop-portal/portals/tuna.portal": (
        "usr/share/xdg-desktop-portal/portals/tuna.portal"
    ),
    "packaging/marlin/tuna-portals.conf": "usr/share/xdg-desktop-portal/tuna-portals.conf",
}
PORTAL_FILES = set(PORTAL_DESTINATIONS)
ICON_ROOT = "share/tuna-desktop/icons"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_archive(path):
    # Inspect only: no extraction, symlink following, or executable input.
    with tarfile.open(path, "r:*") as archive:
        members = {}
        for member in archive:
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and ".." not in name.parts, "unsafe source archive path")
            require(member.name not in members, "duplicate source archive member")
            members[member.name] = member
        return members


def _stage_impl(
    recipe_path,
    factory_path,
    inputs_path,
    source_archive,
    vendor_archive,
    output,
    expected_cells=None,
    require_consumer_adapters=True,
):
    recipe_input = Path(recipe_path).read_bytes()
    factory_input = Path(factory_path).read_bytes()
    json_input = Path(inputs_path).read_bytes()
    admitted = {
        "recipe_input_sha256": hashlib.sha256(recipe_input).hexdigest(),
        "factory_sha256": hashlib.sha256(factory_input).hexdigest(),
        "inputs_sha256": hashlib.sha256(json_input).hexdigest(),
    }
    recipe = yaml.safe_load(recipe_input)
    factory = yaml.safe_load(factory_input)
    inputs = json.loads(json_input)
    require(recipe.get("name") == "tuna-desktop", "wrong recipe")
    sha = inputs.get("source_sha", "")
    require(re.fullmatch(r"[0-9a-f]{40}", sha) is not None, "exact source SHA required")
    require(
        digest(source_archive) == inputs.get("source_archive_sha256"),
        "source archive hash mismatch",
    )
    require(
        digest(vendor_archive) == inputs.get("vendor_archive_sha256"),
        "vendor archive hash mismatch",
    )
    prefix = f"tuna-desktop-{sha}"
    members = read_archive(source_archive)
    required = {f"{prefix}/Cargo.lock"}
    files = recipe.get("install", {}).get("files", [])
    declared = {entry["source"] for entry in files}
    require(PORTAL_FILES <= declared, "portal payload prerequisite missing (PR818)")
    destinations = {entry["source"]: entry["destination"] for entry in files}
    manifest = set(recipe.get("files", {}).get("common", []))
    require(
        all(
            destinations.get(src) == dest and dest in manifest
            for src, dest in PORTAL_DESTINATIONS.items()
        ),
        "portal native file ownership manifest missing",
    )
    require(
        all(
            f"usr/bin/tuna-{binary}" in manifest
            for binary in [
                "compositor",
                "session",
                "shell-host",
                "shell-gtk",
                "ibus-bridge",
                "greeter",
            ]
        ),
        "six session executables must remain native package owned",
    )
    require(
        "usr/share/tuna-desktop/icons" in manifest, "cursor native file ownership manifest missing"
    )
    require(
        any(
            entry.get("source") == ICON_ROOT
            for entry in recipe.get("install", {}).get("directories", [])
        ),
        "licensed cursor payload prerequisite missing (PR818)",
    )
    required |= {f"{prefix}/{path}" for path in declared}
    required |= {
        f"{prefix}/{ICON_ROOT}/Adwaita/{path}"
        for path in [
            "LICENSE-MIT",
            "UPSTREAM-COPYING",
            "index.theme",
            "cursors_scalable/default/metadata.json",
            "cursors_scalable/default/default.svg",
        ]
    }
    require(
        required <= members.keys(), "candidate source missing declared payload/lock/cursor licenses"
    )
    require(
        all(members[name].isfile() for name in required),
        "required source payload must be regular files",
    )
    with tarfile.open(source_archive, "r:*") as archive:
        lock = archive.extractfile(members[f"{prefix}/Cargo.lock"]).read()
    require(
        hashlib.sha256(lock).hexdigest() == inputs.get("lock_sha256"), "source lock hash mismatch"
    )
    vendor = inputs.get("vendor_receipt", {})
    require(
        vendor.get("source_sha") == sha
        and vendor.get("lock_sha256") == inputs["lock_sha256"]
        and vendor.get("archive_sha256") == inputs["vendor_archive_sha256"]
        and vendor.get("offline_metadata_success") is True,
        "exact source/lock/vendor offline receipt required",
    )
    require(
        isinstance(vendor.get("run_id"), int) and vendor["run_id"] > 0,
        "vendor receipt must retain producing CI run",
    )
    require(
        re.fullmatch(
            r"https://github.com/tuna-os/tunaos-packages/releases/download/[^/]+/tuna-desktop-vendor.tar.gz",
            inputs.get("vendor_url", ""),
        )
        is not None,
        "hash-bound vendor GitHub release URL required",
    )
    cells = inputs.get("cells", [])
    if expected_cells is None:
        expected = {
            (consumer, target, arch)
            for consumer, target in CELLS.items()
            for arch in ("x86_64", "aarch64")
        }
        count_message = "six distinct consumer/base/architecture cells required"
    else:
        expected = set(expected_cells)
        count_message = (
            f"{len(expected)} distinct candidate cells required "
            f"({sorted({c for c, _, _ in expected})})"
        )
    require(
        len(cells) == len(expected)
        and {(c.get("consumer"), c.get("target"), c.get("arch")) for c in cells} == expected,
        count_message,
    )
    planned = []
    for cell in cells:
        consumer, target, arch = cell["consumer"], cell["target"], cell["arch"]
        cfg = factory.get("targets", {}).get(target, {})
        require(
            cfg.get("status") == "supported"
            and cfg.get("format") == "rpm"
            and arch in cfg.get("architectures", []),
            f"missing supported native target: {target}/{arch} (PR819)",
        )
        if require_consumer_adapters:
            adapter = factory.get("consumer_adapters", {}).get(consumer, {})
            require(
                adapter.get("target") == target
                and arch in adapter.get("architectures", {}).values(),
                f"wrong consumer adapter: {consumer}/{target}/{arch}",
            )
        image = cell.get("base_image", "")
        probe = cfg.get("probe_images", {}).get(arch) or cfg.get("probe_image", "")
        repository = probe.split("@", 1)[0].rsplit(":", 1)[0]
        require(
            repository and re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", image),
            f"wrong or unpinned base image: {target}/{arch}",
        )
        require(
            ("@sha256:" not in probe or image == probe) and cell.get("base_reference") == probe,
            f"base identity mismatch: {target}/{arch}",
        )
        if require_consumer_adapters:
            platform = next(
                (p for p, a in adapter["architectures"].items() if a == arch), None
            )
        else:
            platform = ARCH_PLATFORMS[arch]
        require(cell.get("platform") == platform, f"platform mismatch: {target}/{arch}")
        baseline = (
            "armv8-a"
            if arch == "aarch64"
            else ("x86-64-v3" if consumer == "skipjack" else "x86-64-v2")
        )
        if require_consumer_adapters:
            require(
                baseline in adapter.get("cpuBaselines", [])
                and cfg.get("cpu_baselines", {}).get(arch, baseline) == baseline,
                f"CPU baseline mismatch: {target}/{arch}",
            )
        else:
            require(
                cfg.get("cpu_baselines", {}).get(arch, baseline) == baseline,
                f"CPU baseline mismatch: {target}/{arch}",
            )
        caps = recipe.get("dependencies", {}).get("build", {}).get("capabilities", [])
        mapped = []
        for cap in caps:
            names = factory.get("dependency_catalog", {}).get(cap, {}).get(target)
            require(isinstance(names, list) and names, f"missing native capability: {target}/{cap}")
            mapped.extend(names)
        candidate = copy.deepcopy(recipe)
        candidate["targets"] = [target]
        candidate["source"] = {
            "url": f"https://github.com/tuna-os/tuna-desktop/archive/{sha}.tar.gz",
            "sha256": inputs["source_archive_sha256"],
            "directory": prefix,
        }
        candidate["sources"] = [
            {
                "url": inputs["vendor_url"],
                "sha256": inputs["vendor_archive_sha256"],
                "filename": "tuna-desktop-vendor.tar.gz",
                "destination": ".",
                "strip_components": 0,
            }
        ]
        candidate["dependencies"]["build"]["targets"] = {target: BUILD}
        candidate["dependencies"]["runtime"]["targets"] = {target: RUNTIME}
        # ELF Requires remain RPM-generated; GTK4-layer-shell is a separate GTK4 input.
        candidate.setdefault("verify", {})["smoke"] += (
            " && command -v canberra-gtk-play"
            " && for schema in org.gnome.desktop.interface org.gnome.desktop.wm.keybindings"
            " org.gnome.desktop.a11y.keyboard org.gnome.desktop.privacy org.gnome.desktop.sound;"
            ' do gsettings list-schemas | grep -qx "$schema" || exit 1; done'
            " && find /usr/lib /usr/lib64 -name libcanberra-pulse.so 2>/dev/null | grep -q ."
            " && test -f /usr/share/sounds/freedesktop/stereo/bell.oga"
            " && test -f /usr/share/sounds/freedesktop/stereo/audio-volume-change.oga"
        )
        key = f"{consumer}-{target}-{arch}"
        recipe_bytes = yaml.safe_dump(candidate, sort_keys=False).encode()
        planned.append(
            (
                key,
                recipe_bytes,
                {
                    **cell,
                    "build_requires": mapped + BUILD,
                    "runtime_requires": RUNTIME,
                    "recipe_sha256": hashlib.sha256(recipe_bytes).hexdigest(),
                    "dependency_status": "unmeasured",
                    "native_install_status": "unmeasured",
                    "build_repositories": cfg.get("build_repositories", []),
                    "system_repositories": cfg.get("system_repositories", []),
                    "cpu_baseline": baseline,
                },
            )
        )
    require(
        digest(source_archive) == inputs["source_archive_sha256"]
        and digest(vendor_archive) == inputs["vendor_archive_sha256"],
        "archive changed during admission",
    )
    require(
        digest(recipe_path) == admitted["recipe_input_sha256"]
        and digest(factory_path) == admitted["factory_sha256"]
        and digest(inputs_path) == admitted["inputs_sha256"],
        "parsed inputs changed during admission",
    )
    out = Path(output)
    require(not out.exists(), "destination already exists; stale staging is forbidden")
    out.mkdir(parents=True)
    for key, recipe_bytes, _ in planned:
        directory = out / key
        directory.mkdir()
        (directory / "package.yaml").write_bytes(recipe_bytes)
    receipt = {
        "schema": 1,
        "candidate_only": True,
        "release_qualified": False,
        "published": False,
        "source_sha": sha,
        "source_archive_sha256": inputs["source_archive_sha256"],
        "vendor_archive_sha256": inputs["vendor_archive_sha256"],
        "lock_sha256": inputs["lock_sha256"],
        "vendor_receipt": vendor,
        **admitted,
        "cells": [entry[2] for entry in planned],
    }
    if expected_cells is not None:
        # Subset receipts say which consumers they cover; the default
        # six-cell receipt is byte-identical to before.
        receipt["cell_subset"] = sorted({consumer for consumer, _, _ in expected})
    (out / "candidate-inputs.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def stage(recipe_path, factory_path, inputs_path, source_archive, vendor_archive, output):
    """Stage the whole six-cell candidate set (unchanged default)."""
    return _stage_impl(
        recipe_path, factory_path, inputs_path, source_archive, vendor_archive, output
    )


def stage_el10_only(
    recipe_path, factory_path, inputs_path, source_archive, vendor_archive, output
):
    """Stage only the skipjack/el10 candidate cells; Alma cells absent, not relabeled."""
    return _stage_impl(
        recipe_path,
        factory_path,
        inputs_path,
        source_archive,
        vendor_archive,
        output,
        expected_cells=EL10_ONLY_CELLS,
        require_consumer_adapters=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("recipe", "factory", "inputs", "source-archive", "vendor-archive", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument(
        "--only-el10",
        action="store_true",
        help="stage only the skipjack/el10 candidate cells (candidate-only subset)",
    )
    args = parser.parse_args()
    entry = stage_el10_only if args.only_el10 else stage
    try:
        entry(
            args.recipe,
            args.factory,
            args.inputs,
            args.source_archive,
            args.vendor_archive,
            args.output,
        )
    except (ValueError, KeyError, TypeError, OSError, tarfile.TarError) as error:
        parser.exit(1, f"candidate staging rejected: {error}\n")


if __name__ == "__main__":
    main()
