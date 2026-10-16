"""A long run script must not contain a ${{ }} expression.

GitHub evaluates a run script that contains ``${{ }}`` as one expression.
When that script is 21000 characters or more, GitHub rejects the whole
workflow, and every workflow that calls it, with "Exceeded max expression
length 21000". No job starts and no log is written; the run only shows
"a workflow file issue".

projectbluefin/utah-packages hit this when one ``${{ }}`` went into a
30000-character container build script. No run script here is that long
yet, so this test stops the first one before it ships. Pass values through
``env:`` instead.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1] / ".github"
LIMIT = 21000


def _definitions() -> list[Path]:
    workflows = (ROOT / "workflows").glob("*.y*ml")
    actions = (ROOT / "actions").glob("*/action.y*ml")
    return sorted([*workflows, *actions])


def _run_scripts(document: dict) -> Iterator[tuple[str, str]]:
    for job_name, job in (document.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            if "run" in step:
                yield f"{job_name}[{index}] {step.get('name', '')}", step["run"]
    for index, step in enumerate((document.get("runs") or {}).get("steps") or []):
        if "run" in step:
            yield f"runs[{index}] {step.get('name', '')}", step["run"]


def test_no_long_run_script_contains_an_expression() -> None:
    definitions = _definitions()
    assert definitions, "found no workflow or action definitions"
    offenders = []
    for path in definitions:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for where, run in _run_scripts(document):
            if "${{" in run and len(run) >= LIMIT:
                offenders.append(f"{path.relative_to(ROOT)}: {where} ({len(run)} characters)")
    assert offenders == [], (
        "GitHub rejects these run scripts as one over-long expression; "
        "pass the values through env: instead:\n" + "\n".join(offenders)
    )
