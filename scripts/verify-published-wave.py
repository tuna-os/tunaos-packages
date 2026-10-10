#!/usr/bin/env python3
"""Assert that a published wave is actually in the SERVED index.

This is the #179 lesson, which publish-tideforge-rpms.yml already carries a
verify job for and publish-build-chain-rpms.yml shipped without: a repo that
was never really published sitting behind a green workflow. Every step can
succeed -- build, sign, index, rclone sync -- and the packages still not be
reachable, because "the workflow exited 0" and "dnf can install it" are
different claims and only the second one matters.

WHAT IT CHECKS

Every binary RPM in the staged wave must appear as a <location href> in the
served repository's primary index. That is stronger than fetching repomd.xml
(which proves a repo exists, not that it contains this wave) and needs no
knowledge of package names, so it works for any family.

TWO THINGS IT HAS TO MODEL

  '+' RENAMING   publish-rpm-wave.sh renames '+' to '.' in filenames, because
                 librepo percent-encodes '+' while the repo.tunaos.org worker
                 looks up raw paths, so those files 404 at install time (run
                 32411090239). The index therefore holds the RENAMED name and
                 a naive comparison against the staged name reports every
                 such package missing.

  SRPMS          excluded from publishing, so they must be excluded here too
                 or every wave looks short.

ON THE URL

The served URL is passed in rather than derived, because write path and read
path are NOT always the same: manifests/package-factory.yaml records that
rpm/el10/x86_64 404s while repo/10/x86_64 serves. For the build-chain
families they do coincide (xfce/10-stream-x86_64, xfce/44-x86_64 and
hummingbird/20251124-* all serve at their r2_path, verified by request), but
deriving it here would bake in an assumption that is false one target over.
"""
from __future__ import annotations

import argparse
import gzip
import re
import sys
import urllib.request
from pathlib import Path

TIMEOUT = 60


def published_names(staged: Path) -> set[str]:
    """Filenames as they will appear in the index, after the '+' rename."""
    return {
        p.name.replace("+", ".")
        for p in staged.rglob("*.rpm")
        if not p.name.endswith(".src.rpm")
    }


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as fh:
        return fh.read()


def primary_href(repomd: bytes) -> str | None:
    """The primary index's location, from repomd.xml."""
    # Deliberately a regex rather than an XML parse: repomd.xml is namespaced
    # and tiny, and the one thing needed is unambiguous in the raw text.
    for block in re.findall(rb'<data[^>]*type="primary"[^>]*>.*?</data>',
                            repomd, re.S):
        m = re.search(rb'<location[^>]*href="([^"]+)"', block)
        if m:
            return m.group(1).decode()
    return None


def indexed_names(served_url: str) -> set[str]:
    base = served_url.rstrip("/") + "/"
    repomd = fetch(base + "repodata/repomd.xml")
    href = primary_href(repomd)
    if not href:
        raise RuntimeError(f"no primary index in {base}repodata/repomd.xml")
    raw = fetch(base + href)
    if href.endswith(".gz"):
        raw = gzip.decompress(raw)
    # Index hrefs are repo-relative paths; only the basename is comparable.
    return {h.decode().rsplit("/", 1)[-1]
            for h in re.findall(rb'<location[^>]*href="([^"]+)"', raw)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--served-url", required=True)
    ap.add_argument("--staged", required=True, type=Path)
    args = ap.parse_args(argv)

    expected = published_names(args.staged)
    if not expected:
        print(f"ERROR: no binary RPMs under {args.staged}; nothing to verify",
              file=sys.stderr)
        return 2

    try:
        served = indexed_names(args.served_url)
    except Exception as exc:
        print(f"ERROR: could not read the served index at "
              f"{args.served_url}: {exc}", file=sys.stderr)
        return 1

    missing = sorted(expected - served)
    print(f"==> {len(expected)} published, {len(served)} in the served index "
          f"at {args.served_url}")
    if missing:
        print(f"ERROR: {len(missing)} published package(s) are NOT in the "
              f"served index -- the workflow went green and the repo did not "
              f"actually receive them:", file=sys.stderr)
        for name in missing[:40]:
            print(f"  {name}", file=sys.stderr)
        if len(missing) > 40:
            print(f"  ... and {len(missing) - 40} more", file=sys.stderr)
        return 1

    print(f"==> all {len(expected)} published package(s) are served")
    return 0


if __name__ == "__main__":
    sys.exit(main())
