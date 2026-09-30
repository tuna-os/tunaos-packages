#!/usr/bin/env python3
"""Assemble a Debian source tree for one rendered Tideforge recipe.

Extracted verbatim from build-tideforge-supported.yml's deb job so the
publish workflow (tunaOS#964) runs the identical assembly -- two inline
copies of this logic would drift, and drift in source assembly is how a
publisher ships something the gate never tested. The gate's deb jobs call
this script too, so there is exactly one copy of the assembly logic.

Usage: assemble-deb-source-tree.py <recipe> <root>
"""
import hashlib
import shutil
import sys
import tarfile
from pathlib import Path
from urllib.request import urlretrieve

import yaml

sys.path.insert(0, "scripts")
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
archive = root / "source.tar.gz"
materialise(recipe["source"], archive, "primary source")
with tarfile.open(archive) as tar:
    tar.extractall(root / "source")
source = root / "source" / recipe["source"].get("directory", f"{recipe['name']}-{recipe['version']}")
if not source.is_dir():
    raise SystemExit(f"declared source directory does not exist: {source}")
# Reconstruct checksum-locked source closures (for example upstream
# submodules) in the same paths used by the RPM/Pacman renderers.
for index, extra in enumerate(recipe.get("sources", []), start=1):
    extra_archive = root / f"source-{index}.tar"
    materialise(extra, extra_archive, f"auxiliary source {index}")
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
