#!/usr/bin/env python3
"""RFC 011's target-parameterized gap-engine entry point.

The engine (scripts/gap_engine.py) carries NO target of its own: this
command names one, the target's `gap_measurement` contract in
manifests/package-factory.yaml supplies its roots manifest and repository
indexes, and the roots manifest supplies its source-tree layout. A new
target is a contract block plus a roots manifest — configuration, never a
fork of the engine.

Usage:
    scripts/measure-target-gap.py --target <id> \
      --report-json docs/<id>-desktop-gap.json \
      --build-order build-order-<id>-desktops.yml
"""
from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from module_loader import load_script  # noqa: E402

# Loads gap_engine.py via shared module loader
_engine = load_script("gap_engine", "gap_engine.py")
def main() -> None:
    if "--target" not in sys.argv:
        raise SystemExit("measure-target-gap.py requires --target")
    _engine.main()


if __name__ == "__main__":
    main()
