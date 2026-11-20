"""A build-order entry's exclude_targets keeps it out of that target's chain.

gtkgreet Requires greetd, which no Alma 10 repository carries, so every
xfce-alma10* cell failed its clean-install verify (run 44751059063) while the
el10 and eln cells, whose repositories do serve greetd, passed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARSE = ROOT / "scripts" / "parse-build-order.py"
XFCE = ROOT / "build-order-xfce.yml"
GTKGREET = "src/xfce-wayland/gtkgreet"


def parse(*args: str) -> str:
    return subprocess.run(
        [sys.executable, str(PARSE), str(XFCE), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def test_manifest_with_exclude_targets_validates():
    parse("--validate")


def test_gtkgreet_is_not_built_for_alma():
    for target in ("alma10", "alma10-kitten"):
        assert GTKGREET not in parse("--all", "--target", target)
        assert GTKGREET not in parse("--tier", "xfce-greeter", "--target", target)


def test_gtkgreet_is_still_built_for_el10_and_eln():
    for target in ("el10", "eln"):
        assert GTKGREET in parse("--all", "--target", target)
    assert GTKGREET in parse("--all")


def test_exclusion_drops_only_gtkgreet():
    full = set(parse("--all").splitlines())
    alma = set(parse("--all", "--target", "alma10").splitlines())
    assert {line.split("\t")[0] for line in full - alma} == {GTKGREET}


def test_cell_runner_passes_its_target_to_the_chain():
    runner = (ROOT / "scripts" / "run-package-factory-cell.sh").read_text(encoding="utf-8")
    chain = (ROOT / "scripts" / "build-chain.sh").read_text(encoding="utf-8")
    assert '--target "$target"' in runner
    assert chain.count('${FACTORY_TARGET:+--target "$FACTORY_TARGET"}') == 2
