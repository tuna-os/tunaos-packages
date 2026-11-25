#!/usr/bin/env python3
"""Reject tracked files over 5 MB under src/.

Policy (tunaos-packages#654): do not commit vendored source tarballs.
Fetch sources at build time from a pinned URL with a sha256 checksum.
History is not rewritten; this check only stops new growth.

Usage: check_src_blob_size.py [--limit-bytes N] [--root DIR]
Exit 1 and list the offenders when any tracked file under src/ is larger.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

LIMIT_BYTES = 5 * 1024 * 1024


def oversized(root: Path, limit: int = LIMIT_BYTES) -> list[tuple[str, int]]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "src"],
        check=True, capture_output=True,
    ).stdout.decode()
    found = []
    for name in filter(None, out.split("\0")):
        path = root / name
        if path.is_file() and not path.is_symlink():
            size = path.stat().st_size
            if size > limit:
                found.append((name, size))
    return sorted(found)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit-bytes", type=int, default=LIMIT_BYTES)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = ap.parse_args()
    bad = oversized(args.root, args.limit_bytes)
    for name, size in bad:
        print(f"{name}: {size / 1048576:.1f} MB exceeds {args.limit_bytes / 1048576:.0f} MB",
              file=sys.stderr)
    if bad:
        print("Do not vendor source tarballs under src/. Fetch at build time with a "
              "pinned URL and sha256 (see CONTRIBUTING.md, tunaos-packages#654).",
              file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
