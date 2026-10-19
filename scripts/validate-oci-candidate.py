#!/usr/bin/env python3
"""Fail-closed inputs for the isolated GHCR publication rehearsal (#912)."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def validate(cell: str, digest: str, run: dict, repository: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,95}", cell):
        raise ValueError("expected exactly one build-chain cell")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("seed must be a sha256 digest, never a tag or empty seed")
    if run.get("repository", {}).get("full_name") != repository:
        raise ValueError("wave must come from this repository")
    if run.get("head_repository", {}).get("full_name") != repository:
        raise ValueError("wave head must belong to this repository")
    if not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", "")):
        raise ValueError("wave must identify an immutable source commit")
    if run.get("path") != ".github/workflows/publish-build-chain-rpms.yml":
        raise ValueError("wave must come from the build-chain publisher")
    if run.get("head_branch") != "main" or run.get("event") != "workflow_dispatch":
        raise ValueError("wave must be a main-branch manual publisher run")
    # A failed build can still bank a useful partial wave. Running/cancelled
    # jobs cannot be the evidence for a reproducible rehearsal.
    if run.get("status") != "completed" or run.get("conclusion") not in {"success", "failure"}:
        raise ValueError("wave run must be completed (success or partial failure)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    try:
        validate(args.cell, args.digest, json.loads(args.run.read_text()), args.repository)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
