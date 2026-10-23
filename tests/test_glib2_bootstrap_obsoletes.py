"""The GNOME 50 GLib bootstrap package must not poison EL10 consumers.

Issue #456 was caused by the bootstrap RPM carrying versioned self-Obsoletes
for glib2 and glib2-devel.  Those entries replace the system packages even
when the TunaOS repository has lower DNF priority.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "src/gnome-50/glib2/glib2-bootstrap.spec"


def test_gnome50_glib2_bootstrap_has_no_versioned_self_obsoletes() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")

    assert not re.search(
        r"^Obsoletes:\s+glib2(?:-devel)?\s+<", text, re.MULTILINE
    ), "the bootstrap RPM must not replace EL10's system GLib packages"


def test_gnome50_glib2_bootstrap_release_supersedes_the_stale_artifact() -> None:
    match = re.search(r"^Release:\s+(\d+)%\{\?dist\}", BOOTSTRAP.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{BOOTSTRAP} has no parseable Release field"
    assert int(match.group(1)) > 1, (
        "the safe bootstrap metadata must use a new release so the stale "
        "self-Obsoletes artifact cannot be reused"
    )
