#!/usr/bin/env python3
"""Atomically update a Tideforge recipe to an upstream stable GitHub release.

Ported hardening from utah-packages' bump pipeline (upstream-version-bumps):
- A changed Version resets the numeric Release to 1; a same-version
  application preserves it.
- Anything the tool cannot move on its own is a SKIP (exit 0 with a reason),
  never an abort: unreachable feed, non-stable latest release, or a source
  layout that is not a plain release tarball (epoch- URLs, commit snapshots).
- Edits are surgical line replacements: comments, key order, and inline
  styles elsewhere in the file are preserved byte-for-byte.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen

import yaml


def get(url: str) -> bytes:
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "tunaos-package-factory"}
    request = Request(url, headers=headers)
    with urlopen(request) as response:  # nosec B310 - URLs are constructed below
        return response.read()


def skip(reason: str) -> int:
    print(f"SKIP: {reason}")
    return 0


def replace_line(text: str, key_pattern: str, replacement: str) -> str:
    """Replace the value of the first `key: <old>` line, keeping indentation."""
    pattern = re.compile(r"^(\s*" + key_pattern + r":\s*).*$", re.M)
    match = pattern.search(text)
    if not match:
        raise SystemExit(f"recipe has no '{key_pattern}' field to update")
    return pattern.sub(lambda m: m.group(1) + replacement, text, count=1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe", type=Path)
    parser.add_argument("--repo", required=True, help="GitHub owner/repository")
    args = parser.parse_args()
    original = args.recipe.read_text()
    recipe = yaml.safe_load(original)
    try:
        release = json.loads(get(f"https://api.github.com/repos/{args.repo}/releases/latest"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return skip(f"cannot reach feed for {args.repo}: {exc}")
    if release.get("prerelease") or release.get("draft"):
        return skip(f"latest GitHub release {release.get('tag_name')} is not stable")
    tag = release["tag_name"]
    version = tag.removeprefix("v")
    source_url = f"https://github.com/{args.repo}/archive/refs/tags/{tag}.tar.gz"
    current_url = ((recipe.get("source") or {}).get("url") or "")
    expected_prefix = f"https://github.com/{args.repo}/archive/refs/tags/"
    if not current_url.startswith(expected_prefix):
        return skip(
            f"custom source layout ({current_url}); not a plain release "
            f"tarball, bump by hand"
        )
    try:
        checksum = hashlib.sha256(get(source_url)).hexdigest()
    except (urllib.error.URLError, OSError) as exc:
        return skip(f"cannot fetch {source_url}: {exc}")
    if recipe["version"] == version and recipe["source"]["sha256"] == checksum:
        print("Recipe already tracks latest stable release")
        return 0
    same_version = str(recipe.get("version")) == version
    updated = original
    updated = replace_line(updated, "version", version if version[0].isdigit() else f"'{version}'")
    # source block fields, in place
    updated = re.sub(
        r"^(?P<indent>\s*)url:\s*\S+",
        lambda m: m.group("indent") + "url: " + source_url,
        updated, count=1, flags=re.M)
    # only the source sha256: the first sha256 after the source url line
    url_pos = updated.index(source_url)
    sha_match = re.compile(r"sha256:\s*[0-9a-f]{64}").search(updated, url_pos)
    if not sha_match:
        raise SystemExit("recipe source block has no sha256 field to update")
    updated = updated[: sha_match.start()] + "sha256: " + checksum + updated[sha_match.end():]
    updated = re.sub(
        r"^(\s*directory:\s*).*$",
        lambda m: m.group(1) + f"{recipe['name']}-{version}",
        updated, count=1, flags=re.M)
    if not same_version and "release" in recipe:
        updated = replace_line(updated, "release", "1")
    args.recipe.write_text(updated)
    print(f"Updated {args.recipe} to {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
