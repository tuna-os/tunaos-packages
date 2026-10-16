"""Unit tests for quarantine enforcement in tideforge-action-cache.py."""

from __future__ import annotations

import importlib.util
import pathlib
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "cache", ROOT / "scripts" / "tideforge-action-cache.py"
)
cache = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cache)


def test_quarantine_check_identifies_quarantined_packages(tmp_path):
    quar_file = tmp_path / "quarantine.yaml"
    quar_data = {
        "schema": 1,
        "quarantined_packages": [
            {
                "package": "problematic-rpm",
                "target": "el10",
                "architecture": "x86_64",
                "reason": "Host timestamp in rpm header",
            }
        ],
    }
    quar_file.write_text(yaml.safe_dump(quar_data))

    assert cache.is_quarantined("problematic-rpm", "el10", "x86_64", quar_file)
    assert not cache.is_quarantined("problematic-rpm", "debian", "amd64", quar_file)
    assert not cache.is_quarantined("clean-rpm", "el10", "x86_64", quar_file)


def test_verify_result_rejects_quarantined_package(tmp_path):
    quar_file = tmp_path / "quarantine.yaml"
    quar_data = {
        "schema": 1,
        "quarantined_packages": [
            {
                "package": "bad-package",
                "target": "el10",
                "architecture": "x86_64",
                "reason": "Divergent digest",
            }
        ],
    }
    quar_file.write_text(yaml.safe_dump(quar_data))

    package_file = tmp_path / "x86_64" / "bad-package.rpm"
    package_file.parent.mkdir(parents=True)
    package_file.write_bytes(b"package content")

    action_key = "sha256:" + "5" * 64
    result = cache.create_result(action_key, [package_file])

    # Clean package passes verification
    cache.verify_result(
        result,
        tmp_path,
        action_key,
        quarantine_manifest=quar_file,
        package="good-package",
        target="el10",
        arch="x86_64",
    )

    # Quarantined package is refused
    with pytest.raises(SystemExit, match="quarantined due to reproducibility divergence"):
        cache.verify_result(
            result,
            tmp_path,
            action_key,
            quarantine_manifest=quar_file,
            package="bad-package",
            target="el10",
            arch="x86_64",
        )
