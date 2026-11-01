#!/usr/bin/env python3
"""Point packages/tuna-desktop at an upstream release tag.

Given a tuna-desktop tag (v1.0.0 or a prerelease such as v1.0.0-beta.1):

1. downloads the tag archive and records its sha256;
2. runs `cargo vendor --locked --versioned-dirs` in it and packs `vendor/`
   as a deterministic tar (sorted names, uid/gid 0, normalized modes, the
   tag commit's time as mtime, `gzip -n -9`), packing twice to prove the
   hash is stable;
3. proves the closure in a fresh extraction of the archive with the recipe's
   own cargo config: `cargo metadata --locked --offline`;
4. compares that asset with release `tuna-desktop-vendor-<tag>` of
   tuna-os/tunaos-packages, creating the release first with --publish, and
   re-downloads the published asset to check its hash;
5. rewrites the recipe in place: version, release, source, vendor asset,
   TUNA_VERSION and the verify smoke's want=, dropping PLACEHOLDER comments.

Edits are surgical line replacements, like bump-github-release-recipe.py;
every one must match exactly once or the script stops without writing.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tideforge  # noqa: E402

UPSTREAM = "tuna-os/tuna-desktop"
PACKAGES = "tuna-os/tunaos-packages"
GITHUB_HOST = "github.com"
RECIPE = ROOT / "packages" / "tuna-desktop" / "package.yaml"
VENDOR_FILENAME = "tuna-desktop-vendor.tar.gz"
TAG = re.compile(r"v([0-9]+\.[0-9]+\.[0-9]+)(?:-([A-Za-z][0-9A-Za-z.]*))?")


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def parse_tag(tag: str) -> tuple[str, str]:
    """`v1.0.0-beta.1` -> (`1.0.0-beta.1` reported by --version, `1.0.0~beta.1` recipe version)."""
    match = TAG.fullmatch(tag)
    if not match:
        fail(f"{tag!r} is not a vX.Y.Z or vX.Y.Z-<prerelease> tag")
    semver = tag[1:]
    version = match.group(1) + (f"~{match.group(2)}" if match.group(2) else "")
    return semver, version


def source_url(tag: str) -> str:
    return f"https://github.com/{UPSTREAM}/archive/refs/tags/{tag}.tar.gz"


def vendor_release(tag: str) -> str:
    return f"tuna-desktop-vendor-{tag}"


def vendor_url(tag: str) -> str:
    return f"https://github.com/{PACKAGES}/releases/download/{vendor_release(tag)}/{VENDOR_FILENAME}"


def _sub_once(text: str, pattern: str, replacement: str, what: str) -> str:
    updated, count = re.subn(pattern, replacement, text, flags=re.M)
    if count != 1:
        fail(f"recipe: expected exactly one {what}, found {count}")
    return updated


def rewrite_recipe(text: str, tag: str, source_sha256: str, vendor_sha256: str) -> str:
    """The recipe text pointed at `tag`, with the given archive hashes."""
    semver, version = parse_tag(tag)
    for name, value in (("source", source_sha256), ("vendor", vendor_sha256)):
        if not re.fullmatch(r"[0-9a-f]{64}", value) or value == "0" * 64:
            fail(f"{name} sha256 must be a real lowercase SHA-256, got {value!r}")
    old = yaml.safe_load(text)
    text = re.sub(r"^# PLACEHOLDER:.*\n(?:#.*\n)*", "", text, flags=re.M)
    text = _sub_once(text, r'^version: .*$', f'version: "{version}"', "top-level version")
    if str(old["version"]) != version:
        text = _sub_once(text, r"^release: .*$", "release: 1", "top-level release")
    text = _sub_once(
        text,
        r"^source:\n  url: .*\n  sha256: .*\n  directory: .*$",
        f'source:\n  url: {source_url(tag)}\n  sha256: "{source_sha256}"\n  directory: tuna-desktop-{semver}',
        "source block (url, sha256, directory)",
    )
    text = _sub_once(
        text,
        rf"^  - url: https://github\.com/{re.escape(PACKAGES)}/releases/download/[^/\s]+/{re.escape(VENDOR_FILENAME)}\n    sha256: .*$",
        f'  - url: {vendor_url(tag)}\n    sha256: "{vendor_sha256}"',
        "vendor asset source",
    )
    text = _sub_once(text, r"^    TUNA_VERSION: .*$", f"    TUNA_VERSION: {tag}", "TUNA_VERSION")
    text = _sub_once(text, r"^    want=[^;\s]*;$", f"    want={semver};", "verify smoke want=")
    if "PLACEHOLDER" in text:
        fail("recipe still mentions PLACEHOLDER after the rewrite")
    return text


def check_recipe(recipe: dict) -> None:
    """The recipe's version, TUNA_VERSION, smoke and source agree on one tag."""
    tag = recipe["build"]["environment"]["TUNA_VERSION"]
    semver, version = parse_tag(tag)
    if str(recipe["version"]) != version:
        fail(f"version {recipe['version']!r} does not match TUNA_VERSION {tag} (expected {version!r})")
    if f"want={semver};" not in recipe["verify"]["smoke"]:
        fail(f"verify smoke does not expect {semver}")
    if recipe["source"]["url"] != source_url(tag) or recipe["source"]["directory"] != f"tuna-desktop-{semver}":
        fail(f"source is not the {tag} tag archive")
    vendor = [item for item in recipe["sources"] if item.get("filename") == VENDOR_FILENAME]
    if len(vendor) != 1 or vendor[0]["url"] != vendor_url(tag):
        fail(f"vendor asset is not {vendor_release(tag)}")
    for target in recipe["targets"]:
        tideforge.validate(recipe, target)


def download(url: str, destination: Path) -> str:
    request = Request(url, headers={"User-Agent": "tunaos-package-factory"})
    digest = hashlib.sha256()
    with urlopen(request) as response, destination.open("wb") as out:  # nosec B310 - fixed GitHub URLs
        for block in iter(lambda: response.read(1 << 20), b""):
            digest.update(block)
            out.write(block)
    return digest.hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(archive: Path, destination: Path, top: str) -> tuple[Path, int]:
    """Extract the tag archive; return its root and the commit time GitHub stamps on it."""
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        root = next((m for m in tar.getmembers() if m.name.rstrip("/") == top), None)
        if root is None or not root.isdir():
            fail(f"tag archive has no top-level {top}/")
        tar.extractall(destination, filter="data")
    return destination / top, int(root.mtime)


def pack_vendor(source: Path, epoch: int, output: Path) -> None:
    subprocess.run(
        f"set -o pipefail; tar --sort=name --format=gnu --owner=0 --group=0 --numeric-owner --mtime=@{epoch}"
        " --mode=u+rwX,go+rX,go-w -C . -cf - vendor | gzip -n -9 > " + str(output),
        shell=True, check=True, cwd=source, executable="/bin/bash",
    )


def offline_metadata(archive: Path, vendor: Path, top: str, cargo_config: str, workdir: Path) -> None:
    tree, _ = extract(archive, workdir, top)
    with tarfile.open(vendor) as tar:
        tar.extractall(tree, filter="data")
    (tree / ".cargo").mkdir(exist_ok=True)
    (tree / ".cargo" / "config.toml").write_text(cargo_config)
    subprocess.run(
        ["cargo", "metadata", "--locked", "--offline", "--format-version", "1"],
        cwd=tree, check=True, stdout=subprocess.DEVNULL,
    )


def release_asset_sha(tag: str, workdir: Path) -> str | None:
    result = subprocess.run(
        ["gh", "api", "--hostname", GITHUB_HOST, "--method", "GET",
         f"repos/{PACKAGES}/releases/tags/{quote(vendor_release(tag), safe='')}",
         "--include", "--silent"],
        stdout=subprocess.PIPE, text=True,
    )
    statuses = re.findall(r"^HTTP/\S+ (\d{3})[^\r\n]*", result.stdout, flags=re.M)
    status = int(statuses[-1]) if statuses else None
    if status == 404 and result.returncode != 0:
        return None
    if result.returncode != 0 or status != 200:
        fail(f"vendor release lookup failed (HTTP {status or 'unknown'}, gh exit {result.returncode})")
    return download(vendor_url(tag), workdir / "published-vendor.tar.gz")


def publish(tag: str, vendor: Path, digest: str) -> None:
    notes = (
        f"Immutable offline Cargo dependencies for Tuna Desktop tag {tag} ({UPSTREAM}). "
        "Generated with `cargo vendor --locked --versioned-dirs`, packed as a deterministic tar "
        "(sorted names, uid/gid 0, normalized modes, the tag commit's time as mtime, `gzip -n -9`) "
        "by scripts/prepare-tuna-desktop-release.py. Offline locked metadata resolution "
        "(`cargo metadata --locked --offline`) passed against the vendor directory. This asset "
        "contains source dependencies, not an installable desktop release.\n\n"
        f"SHA-256: `{digest}`."
    )
    result = subprocess.run(
        ["gh", "api", "--hostname", GITHUB_HOST, "--method", "POST",
         f"repos/{PACKAGES}/releases", "--input", "-"],
        input=json.dumps({
            "tag_name": vendor_release(tag),
            "name": f"tuna-desktop vendor {tag}",
            "body": notes,
            "draft": False,
            "prerelease": False,
            "make_latest": "false",
        }),
        text=True, stdout=subprocess.PIPE, check=True,
    )
    release = json.loads(result.stdout)
    upload_url = release["upload_url"].split("{", 1)[0]
    if upload_url != f"https://uploads.github.com/repos/{PACKAGES}/releases/{release['id']}/assets":
        fail("created release returned an unexpected upload URL")
    subprocess.run(
        ["gh", "api", "--hostname", GITHUB_HOST, "--method", "POST",
         f"{upload_url}?name={quote(VENDOR_FILENAME, safe='')}",
         "--header", "Content-Type: application/gzip", "--input", str(vendor)],
        check=True, stdout=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tag", help="upstream tag, e.g. v1.0.0-beta.1")
    parser.add_argument("--recipe", type=Path, default=RECIPE)
    parser.add_argument("--publish", action="store_true",
                        help=f"create release {vendor_release('<tag>')} when it does not exist yet")
    parser.add_argument("--keep", type=Path, help="work directory to keep (default: temporary)")
    args = parser.parse_args()
    semver, _ = parse_tag(args.tag)
    top = f"tuna-desktop-{semver}"
    original = args.recipe.read_text()
    cargo_config = yaml.safe_load(original)["build"]["cargo_config"]
    workdir = args.keep or Path(tempfile.mkdtemp(prefix="tuna-desktop-release-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        archive = workdir / f"{top}.tar.gz"
        source_sha = download(source_url(args.tag), archive)
        print(f"source {source_url(args.tag)}\n  sha256 {source_sha}")
        tree, epoch = extract(archive, workdir / "vendor-build", top)
        subprocess.run(["cargo", "vendor", "--locked", "--versioned-dirs", "vendor"],
                       cwd=tree, check=True, stdout=subprocess.DEVNULL)
        vendor = workdir / VENDOR_FILENAME
        pack_vendor(tree, epoch, vendor)
        vendor_sha = sha256(vendor)
        pack_vendor(tree, epoch, workdir / "repack.tar.gz")
        if sha256(workdir / "repack.tar.gz") != vendor_sha:
            fail("vendor tar is not reproducible: two packs of the same tree differ")
        offline_metadata(archive, vendor, top, cargo_config, workdir / "offline-check")
        print(f"vendor {VENDOR_FILENAME}\n  sha256 {vendor_sha} (reproducible, offline metadata OK)")
        published = release_asset_sha(args.tag, workdir)
        if published is None:
            if not args.publish:
                fail(f"release {vendor_release(args.tag)} does not exist; rerun with --publish "
                     f"to upload {vendor} (kept with --keep)")
            publish(args.tag, vendor, vendor_sha)
            published = release_asset_sha(args.tag, workdir)
        if published != vendor_sha:
            fail(f"published {vendor_release(args.tag)} asset has sha256 {published}, built {vendor_sha}")
        print(f"published {vendor_url(args.tag)} matches")
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
    updated = rewrite_recipe(original, args.tag, source_sha, vendor_sha)
    check_recipe(yaml.safe_load(updated))
    args.recipe.write_text(updated)
    # manifests/catalog.yaml records each recipe's version and source hash;
    # tests/test_catalog_completeness.py fails while it is stale.
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build-catalog.py")], check=True)
    print(f"Updated {args.recipe} and manifests/catalog.yaml to {args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
