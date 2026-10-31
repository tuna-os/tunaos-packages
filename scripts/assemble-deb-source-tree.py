#!/usr/bin/env python3
"""Assemble a Debian source tree for one rendered Tideforge recipe.

Extracted verbatim from build-tideforge-supported.yml's deb job so the
publish workflow (tunaOS#964) runs the identical assembly -- two inline
copies of this logic would drift, and drift in source assembly is how a
publisher ships something the gate never tested. The gate's deb jobs call
this script too, so there is exactly one copy of the assembly logic.

Usage: assemble-deb-source-tree.py <recipe> <root> [<target>]

With a target, an auxiliary source that declares `targets` is assembled only
for those targets, as the RPM and PKGBUILD renderers do
(tideforge.source_entries).
"""
import hashlib
import shutil
import sys
import tarfile
from email.parser import Parser
from pathlib import Path
from urllib.request import urlretrieve

import yaml

sys.path.insert(0, "scripts")
import tideforge
import tideforge_cache

cache_dir = Path.home() / ".cache" / "tideforge" / "sources"
def materialise(source, destination, label):
    payload = tideforge_cache.lookup(cache_dir, source["sha256"])
    if payload is not None:
        destination.write_bytes(payload)
        print(f"source cache hit: {label}")
        return
    urlretrieve(source["url"], destination)
    if hashlib.file_digest(destination.open("rb"), "sha256").hexdigest() != source["sha256"]:
        raise SystemExit(f"checksum mismatch for {label}")
    tideforge_cache.store(cache_dir, destination.read_bytes())
    print(f"source cache miss, downloaded: {label}")
recipe = yaml.safe_load(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
target = sys.argv[3] if len(sys.argv) > 3 else None
# Native mode must be validated before extracting or replacing any packaging.
native = tideforge.native_deb_source(recipe)
if native is not None:
    tideforge.validate(recipe, target)
archive = root / "source.tar.gz"
materialise(recipe["source"], archive, "primary source")
with tarfile.open(archive) as tar:
    tar.extractall(root / "source")
source = root / "source" / recipe["source"].get("directory", f"{recipe['name']}-{recipe['version']}")
if not source.is_dir():
    raise SystemExit(f"declared source directory does not exist: {source}")
materialised_sources = {}
# Reconstruct checksum-locked source closures (for example upstream
# submodules) in the same paths used by the RPM/Pacman renderers.
for index, extra in enumerate(recipe.get("sources", []), start=1):
    if target and "targets" in extra and target not in extra["targets"]:
        continue
    extra_archive = root / f"source-{index}.tar"
    materialise(extra, extra_archive, f"auxiliary source {index}")
    materialised_sources[tideforge.source_filename(extra, index)] = extra_archive
    destination = source / extra["destination"]
    if not extra.get("extract", True):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(extra_archive, destination)
        continue
    destination.mkdir(parents=True, exist_ok=True)
    strip = extra.get("strip_components", 1)
    with tarfile.open(extra_archive) as tar:
        members = []
        for member in tar.getmembers():
            parts = Path(member.name).parts[strip:]
            if not parts or ".." in parts or Path(*parts).is_absolute():
                continue
            member.name = str(Path(*parts))
            members.append(member)
        tar.extractall(destination, members=members, filter="data")
(root / "source-dir").write_text("/work/" + source.relative_to(root).as_posix() + "\n")
if native is not None:
    descriptor = materialised_sources[native["dsc"]]
    signed = descriptor.read_text()
    if not signed.startswith("-----BEGIN PGP SIGNED MESSAGE-----\n"):
        raise SystemExit("native source dsc is not a signed source descriptor")
    fields = Parser().parsestr(
        signed.split("\n\n", 1)[1].split("\n-----BEGIN PGP SIGNATURE-----", 1)[0]
    )
    if (fields.get("Source"), fields.get("Version"), fields.get("Format")) != (
        recipe["name"], native["version"], "3.0 (quilt)"
    ):
        raise SystemExit("native source dsc identity does not match the recipe")
    orig_name = tideforge.source_filename(recipe["source"], 0)
    expected = {
        orig_name: archive,
        native["debian_tar"]: materialised_sources[native["debian_tar"]],
    }
    checksums = {}
    for line in fields.get("Checksums-Sha256", "").splitlines():
        if line.strip():
            checksum, size, filename = line.split()
            if filename in checksums:
                raise SystemExit("duplicate native dsc source filename")
            checksums[filename] = (checksum, int(size))
    if set(checksums) != set(expected):
        raise SystemExit(
            "native source dsc closure does not match the locked orig and Debian archives"
        )
    for filename, payload in expected.items():
        if checksums[filename] != (
            hashlib.sha256(payload.read_bytes()).hexdigest(), payload.stat().st_size
        ):
            raise SystemExit(f"native source dsc checksum/size mismatch: {filename}")
    debian = source / "debian"
    changelog = debian / "changelog"
    if not changelog.read_text().startswith(
        f"{recipe['name']} ({native['version']}) {native['suite']};"
    ):
        raise SystemExit("native Debian changelog identity mismatch")
    patch_name = Path(native["patch"]["path"]).name
    patch = root / "rendered" / "debian" / "patches" / patch_name
    if hashlib.sha256(patch.read_bytes()).hexdigest() != native["patch"]["sha256"]:
        raise SystemExit("rendered native patch checksum mismatch")
    if (debian / "patches" / patch_name).exists():
        raise SystemExit("native source already contains the downstream patch")
    rendered_files = {
        path.relative_to(root / "rendered").as_posix()
        for path in (root / "rendered").rglob("*")
        if path.is_file()
    }
    if rendered_files != {f"debian/patches/{patch_name}", "native-changelog"}:
        raise SystemExit(
            "native renderer may only add its maintained quilt patch and changelog entry"
        )
    # Preserve the original closure alongside the changed source, so -S -sa
    # can reuse the exact orig and evidence retains the original signed dsc.
    originals = source.parent / "original-source"
    originals.mkdir()
    for filename, payload in {**expected, native["dsc"]: descriptor}.items():
        shutil.copyfile(payload, originals / filename)
    shutil.copyfile(archive, source.parent / orig_name)
    shutil.rmtree(source / ".tuna-source")
    shutil.copyfile(patch, debian / "patches" / patch_name)
    series = debian / "patches" / "series"
    original_series = series.read_text()
    series.write_text(
        original_series + ("" if original_series.endswith("\n") else "\n") + patch_name + "\n"
    )
    changelog.write_text((root / "rendered" / "native-changelog").read_text() + changelog.read_text())
    raise SystemExit(0)
# Tideforge owns the packaging. pop-os sources ship their own
# in-tree debian/ directory, and overlaying rendered files onto it
# mixes two packagings: upstream's debian/compat collided with our
# debhelper-compat build-dependency and dh refused to run at all
# ("compat level specified both in debian/compat and in
# debian/control" -- pop-icon-theme's first deb cells, run
# 30692954918). Clear it before rendering.
shutil.rmtree(source / "debian", ignore_errors=True)
(source / "debian").mkdir(parents=True, exist_ok=True)
for path in (root / "rendered" / "debian").rglob("*"):
    if path.is_file():
        target = source / "debian" / path.relative_to(root / "rendered" / "debian")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        if path.name == "rules":
            target.chmod(0o755)
