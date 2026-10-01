"""Unit tests for reproducibility verification section in scripts/factory-status.py."""

from __future__ import annotations

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "factory_status", ROOT / "scripts" / "factory-status.py"
)
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)


def test_factory_status_renders_reproducibility_section():
    report = {
        "measured_at": "2026-09-30T11:00:00+00:00",
        "targets": {},
        "unmeasured_targets": {},
        "reproducibility": {
            "summary": {
                "sampled": 10,
                "reproducible": 9,
                "divergent": 1,
                "quarantined": 1,
                "reproducibility_rate": 90.0,
            },
            "by_coordinate": {
                "tideforge/deb/amd64": {
                    "sampled": 5,
                    "reproducible": 5,
                    "divergent": 0,
                    "rate": 100.0,
                },
                "tideforge/rpm/x86_64": {
                    "sampled": 5,
                    "reproducible": 4,
                    "divergent": 1,
                    "rate": 80.0,
                },
            },
            "quarantined": [
                {
                    "package": "divergent-pkg",
                    "target": "el10",
                    "architecture": "x86_64",
                    "reason": "sha256 digest mismatch",
                }
            ],
        },
    }

    rendered = fs.render(report)
    assert "## Reproducibility verification" in rendered
    assert "90.0%" in rendered
    assert "| tideforge (amd64) | deb | 5 | 5 | 0 | 100.0% |" in rendered
    assert "| tideforge (x86_64) | rpm | 5 | 4 | 1 | 80.0% |" in rendered
    assert "divergent-pkg" in rendered
    assert "sha256 digest mismatch" in rendered


def test_factory_status_without_reproducibility_renders_cleanly():
    report = {
        "measured_at": "2026-09-30T11:00:00+00:00",
        "targets": {},
        "unmeasured_targets": {},
    }
    rendered = fs.render(report)
    assert "# Factory status — built vs needed, per target" in rendered
    assert "## Reproducibility verification" not in rendered
