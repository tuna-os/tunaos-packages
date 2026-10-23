"""Compare real dpkg payload extraction with an installed package root."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deb-installed-payload.sh"


@pytest.fixture
def payload(tmp_path: Path) -> tuple[Path, Path, Path]:
    if not shutil.which("dpkg-deb"):
        pytest.fail("actual dpkg-deb is required for payload tests")
    package_root = tmp_path / "package"
    control = package_root / "DEBIAN/control"
    control.parent.mkdir(parents=True)
    control.parent.chmod(0o755)
    control.write_text(
        "Package: tuna-fixture\nVersion: 1.0-1\nArchitecture: all\n"
        "Maintainer: tests <tests@example.invalid>\nDescription: payload fixture\n",
        encoding="utf-8",
    )
    executable = package_root / "usr/bin/tuna-compositor"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"verified payload bytes\n")
    executable.chmod(0o755)
    config = package_root / "usr/share/xdg-desktop-portal/tuna-portals.conf"
    config.parent.mkdir(parents=True)
    config.write_text("[preferred]\n", encoding="utf-8")
    (package_root / "usr/bin/roost-compositor").symlink_to("tuna-compositor")
    archive = tmp_path / "tuna-fixture.deb"
    subprocess.run(["dpkg-deb", "--build", str(package_root), str(archive)], check=True)
    installed = tmp_path / "installed"
    installed.mkdir()
    subprocess.run(["dpkg-deb", "--extract", str(archive), str(installed)], check=True)
    return archive, installed, tmp_path / "installed-payload.tsv"


def run(payload: tuple[Path, Path, Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *(str(item) for item in payload)],
        capture_output=True,
        text=True,
    )


def test_archive_matches_actual_installed_bytes_and_links(payload):
    result = run(payload)
    assert result.returncode == 0, result.stderr
    receipt = payload[2].read_text(encoding="utf-8")
    assert "file\tusr/bin/tuna-compositor\t755\t" in receipt
    assert "symlink\tusr/bin/roost-compositor\ttuna-compositor" in receipt


@pytest.mark.parametrize("change", ["bytes", "missing", "mode", "symlink", "config"])
def test_changed_payload_rejects_and_removes_stale_receipt(payload, change):
    archive, installed, receipt = payload
    receipt.write_text("previous passing receipt\n", encoding="utf-8")
    executable = installed / "usr/bin/tuna-compositor"
    link = installed / "usr/bin/roost-compositor"
    if change == "bytes":
        executable.write_bytes(b"changed\n")
    elif change == "missing":
        executable.unlink()
    elif change == "mode":
        executable.chmod(0o644)
    elif change == "symlink":
        link.unlink()
        link.symlink_to("/usr/bin/foreign")
    else:
        (installed / "usr/share/xdg-desktop-portal/tuna-portals.conf").unlink()
    result = run((archive, installed, receipt))
    assert result.returncode != 0
    assert not receipt.exists()
    assert not list(receipt.parent.glob("installed-payload.tsv.tmp.*"))
