"""The drift workflow must install every module the measure script imports.

gap_engine.py imports `zstandard` lazily, inside decompress(),
and only when a repository's primary index is .zst — which the Fedora Rawhide
reference always is.  A workflow that installs only PyYAML passes the drift
gate, downloads repomd, and then dies on the import: run 32017727489, the
detector's first scheduled firing, failed exactly there in 19 seconds.

Lazy imports dodge module-level import scans, so this test reads the script
for its full import surface (top-level and function-local) and asserts the
workflow's pip install covers every non-stdlib name.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gap_engine.py"
WORKFLOWS = [
    ROOT / ".github" / "workflows" / "gap-drift.yml",
    ROOT / ".github" / "workflows" / "upstream-drift.yml",
]

# import name -> pip distribution name
NON_STDLIB = {"yaml": "PyYAML", "zstandard": "zstandard"}


def script_imports() -> set[str]:
    names = set()
    for match in re.finditer(
        r"^\s*(?:import\s+(\w+)|from\s+(\w+)\s+import)",
        SCRIPT.read_text(encoding="utf-8"),
        re.M,
    ):
        names.add(match.group(1) or match.group(2))
    return names


def test_script_import_surface_is_known() -> None:
    """A new non-stdlib import must be added to NON_STDLIB (and the workflow)."""
    unknown = {
        name
        for name in script_imports()
        if name not in NON_STDLIB and name not in sys.stdlib_module_names
    }
    assert not unknown, (
        f"gap_engine.py imports {sorted(unknown)} which this test "
        "does not classify; add each to NON_STDLIB and to the drift workflow's "
        "pip install"
    )


def test_workflow_installs_every_non_stdlib_import() -> None:
    needed = {NON_STDLIB[n] for n in script_imports() if n in NON_STDLIB}
    for workflow in WORKFLOWS:
        body = workflow.read_text(encoding="utf-8")
        install_lines = [line for line in body.splitlines()
                         if "pip install" in line]
        assert install_lines, f"{workflow.name} has no pip install step"
        for dist in sorted(needed):
            assert any(dist in line for line in install_lines), (
                f"{workflow.name}'s pip install is missing {dist}; the measure "
                "script imports it (possibly lazily) and the run will die at that "
                "import, as run 32017727489 did for zstandard"
            )
