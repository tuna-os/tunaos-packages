"""Actual libarchive extraction and installed bytes, not command mocks."""

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def payload(tmp_path):
    if not shutil.which("bsdtar"):
        pytest.fail("actual bsdtar is required for installed-payload tests")
    archive = tmp_path / "candidate.tar"
    with tarfile.open(archive, "w") as stream:
        for name, data, mode in [
            ("usr/bin/tuna-compositor", b"actual fixture bytes", 0o755),
            ("usr/share/xdg-desktop-portal/tuna-portals.conf", b"[preferred]\n", 0o644),
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = mode
            stream.addfile(member, io.BytesIO(data))
        member = tarfile.TarInfo("usr/bin/roost-compositor")
        member.type = tarfile.SYMTYPE
        member.linkname = "tuna-compositor"
        stream.addfile(member)
    installed = tmp_path / "installed root"
    installed.mkdir()
    subprocess.run(["bsdtar", "-xf", str(archive), "-C", str(installed)], check=True)
    return archive, installed, tmp_path / "receipt.tsv"


def run(payload):
    return subprocess.run(
        ["bash", str(ROOT / "scripts/arch-installed-payload.sh"), *map(str, payload)],
        text=True,
        capture_output=True,
    )


def test_archive_matches_actual_installed_bytes_and_symlink(payload):
    result = run(payload)
    assert result.returncode == 0, result.stderr
    receipt = payload[2].read_text()
    assert "file\tusr/bin/tuna-compositor\t755\t" in receipt
    assert "symlink\tusr/bin/roost-compositor\ttuna-compositor" in receipt


@pytest.mark.parametrize("change", ["bytes", "missing", "mode", "symlink", "directory"])
def test_changed_actual_payload_rejects_and_removes_stale_receipt(payload, change):
    archive, installed, receipt = payload
    receipt.write_text("previous passing receipt")
    executable = installed / "usr/bin/tuna-compositor"
    link = installed / "usr/bin/roost-compositor"
    if change == "bytes":
        executable.write_bytes(b"changed")
    elif change == "missing":
        executable.unlink()
    elif change == "mode":
        executable.chmod(0o644)
    elif change == "symlink":
        link.unlink()
        link.symlink_to("/usr/bin/foreign")
    else:
        (installed / "usr/share/xdg-desktop-portal/tuna-portals.conf").unlink()
    result = run(payload)
    assert result.returncode != 0
    assert not receipt.exists()
    assert not list(receipt.parent.glob("receipt.tsv.tmp.*"))
