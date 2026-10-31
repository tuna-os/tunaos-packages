#!/usr/bin/env python3
"""Emit createrepo_c -x glob patterns for one target's withdrawn packages.

The thin CLI half of withdrawn_packages.py: a publish job knows its target
name (matrix.target in publish-build-chain-rpms.yml, the TARGET env var
fanout-publish-one.sh's caller sets) and nothing about the manifest's shape.
This turns one into the other and writes a file publish-rpm-wave.sh's
--exclude-file reads, one pattern per line -- never printed to stdout mixed
with anything else, so a caller can redirect it directly without parsing.

Usage:
    scripts/plan-withdrawn-excludes.py --target hummingbird --out /tmp/excludes.txt
    scripts/plan-withdrawn-excludes.py --target hummingbird   # prints to stdout
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import withdrawn_packages  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--manifest", type=pathlib.Path,
                     default=withdrawn_packages.DEFAULT_MANIFEST)
    ap.add_argument("--out", type=pathlib.Path,
                     help="write patterns here (default: stdout)")
    args = ap.parse_args(argv)

    targets = withdrawn_packages.load(args.manifest)
    patterns = withdrawn_packages.excludes_for(targets, args.target)
    text = "".join(f"{p}\n" for p in patterns)

    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"==> {len(patterns)} withdrawn pattern(s) for {args.target} "
              f"written to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
