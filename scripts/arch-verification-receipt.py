#!/usr/bin/env python3
"""Bind served/installed Tuna bytes to the publisher's verified ActionResult.

This consumes observations only after arch-verify-published.sh has installed
the package and verified its served detached signature. It never treats a
matching version as proof that the current recipe produced those bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

SCRIPT_ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "receipt_cache", SCRIPT_ROOT / "tideforge-action-cache.py"
)
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)
SIGNER = "4E5CC9F8B3B521793D95266E629BE6EA45188366"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def receipt(
    root: Path,
    provenance: Path,
    observations: Path,
    arch: str,
    revision: str,
    run_url: str,
    served_url: str,
    run_attempt: int = 1,
) -> dict:
    require(isinstance(run_attempt, int) and run_attempt > 0, "positive run attempt required")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", revision)), "exact publisher revision required")
    checkout = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    require(
        checkout.returncode == 0 and checkout.stdout.strip() == revision,
        "publisher revision does not match the verification checkout",
    )
    parsed = urlparse(run_url)
    require(
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and re.fullmatch(r"/tuna-os/tunaos-packages/actions/runs/[0-9]+", parsed.path) is not None
        and not parsed.query
        and not parsed.fragment,
        "publisher run URL required",
    )
    served = urlparse(served_url)
    require(
        served.scheme == "https"
        and served.netloc == "repo.tunaos.org"
        and served.path.rstrip("/") == f"/pacman/arch/{arch}"
        and not served.query
        and not served.fragment,
        "unexpected served repository",
    )
    recipe_path = root / "packages/tuna-desktop/package.yaml"
    recipe = yaml.safe_load(recipe_path.read_text())
    identity = json.loads((provenance / "action-inputs.json").read_text())
    result = json.loads((provenance / "action-result.json").read_text())
    inputs = identity["inputs"]
    expected = cache.action_inputs(
        argparse.Namespace(
            root=str(root),
            recipe=str(recipe_path),
            factory=str(root / "manifests/package-factory.yaml"),
            target="arch",
            arch=arch,
            image=inputs["build_image"],
            source_date_epoch=inputs["reproducibility"]["source_date_epoch"],
            dependency_key=[],
        )
    )
    require(inputs == expected, "build inputs do not match the current recipe/architecture")
    key = cache.action_key(expected)
    require(
        identity["action_key"] == key
        and result["action_key"] == key
        and result["schema"] == cache.SCHEMA,
        "ActionResult identity mismatch",
    )
    entries = result["artifacts"]
    require(isinstance(entries, list) and bool(entries), "nonempty package artifacts required")
    version = f"{recipe['version']}-{recipe['release']}"
    filename = f"tuna-desktop-{version}-{arch}.pkg.tar.zst"
    names = []
    for entry in entries:
        names.append(cache.safe_artifact_name(entry["name"]))
        cache.require_sha256(entry["digest"], "package digest")
        require(type(entry["size"]) is int and entry["size"] > 0, "invalid package size")
    require(len(names) == len(set(names)), "ambiguous duplicate artifact names")
    require(filename in names, "expected Tuna package artifact missing")
    artifact = entries[names.index(filename)]
    digest = artifact["digest"]
    fields = (observations / "installed.tsv").read_text().strip().split("\t")
    require(len(fields) == 6, "missing or malformed installed observations")
    name, installed_version, installed_arch, observed_filename, observed_hash, observed_size = (
        fields
    )
    require(
        (name, installed_version, installed_arch, observed_filename)
        == ("tuna-desktop", version, arch, filename),
        "installed package identity mismatch",
    )
    require(
        "sha256:" + observed_hash == digest and observed_size == str(artifact["size"]),
        "served bytes differ from the verified build artifact",
    )
    status = (observations / "signature-status.txt").read_text().splitlines()
    valid = [line.split() for line in status if line.startswith("[GNUPG:] VALIDSIG ")]
    require(
        len(valid) == 1
        and len(valid[0]) in (11, 12)
        and re.fullmatch(r"[0-9A-F]{40}", valid[0][2]) is not None
        and valid[0][10] == "00"
        and (valid[0][2] == SIGNER or valid[0][-1] == SIGNER),
        "served signature is not from the pinned repository signer",
    )
    require(
        not any(
            line.startswith(f"[GNUPG:] {tag} ")
            for line in status
            for tag in (
                "BADSIG",
                "ERRSIG",
                "REVKEYSIG",
                "EXPKEYSIG",
                "EXPSIG",
                "NODATA",
                "NO_PUBKEY",
                "FAILURE",
                "ERROR",
            )
        ),
        "served signature is invalid or expired",
    )
    signature = observations / "signature.sig"
    require(
        signature.is_file() and signature.stat().st_size > 0, "served detached signature missing"
    )
    sources = [recipe["source"], *recipe.get("sources", [])]
    require(
        all(re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) for source in sources),
        "source checksums must be pinned",
    )
    return {
        "schema": 1,
        "status": "pass",
        "revision": revision,
        "run_url": run_url,
        "run_attempt": run_attempt,
        "generated": datetime.now(UTC).isoformat(),
        "package": {
            "name": name,
            "version": version,
            "architecture": arch,
            "filename": filename,
            "sha256": observed_hash,
            "size": artifact["size"],
            "url": served_url.rstrip("/") + "/" + filename,
        },
        "source_inputs": sources,
        "action_key": key,
        "build_inputs": expected,
        "signature": {
            "primary_fingerprint": SIGNER,
            "sha256": hashlib.sha256(signature.read_bytes()).hexdigest(),
            "url": served_url.rstrip("/") + "/" + filename + ".sig",
        },
        "verification": {"served_install": "pass", "exact_artifact": "pass", "signature": "pass"},
        "artifacts": [
            "installed.tsv",
            "signature-status.txt",
            "signature.sig",
            "action-inputs.json",
            "action-result.json",
        ],
        "note": (
            "Package publication proof only; desktop parity, image boot, "
            "upgrade and recovery require separate qualification."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--arch", required=True, choices=("x86_64", "aarch64"))
    parser.add_argument("--revision", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--run-attempt", type=int, default=1)
    parser.add_argument("--served-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # A failed recheck must not leave yesterday's passing receipt at the path.
    args.output.unlink(missing_ok=True)
    try:
        value = receipt(
            args.root,
            args.provenance,
            args.observations,
            args.arch,
            args.revision,
            args.run_url,
            args.served_url,
            args.run_attempt,
        )
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"verification receipt refused: {exc}\n")
    args.output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
