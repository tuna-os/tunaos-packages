"""No tracked file under src/ may exceed 5 MB (tunaos-packages#654)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.check_src_blob_size import LIMIT_BYTES, oversized

ROOT = Path(__file__).resolve().parents[1]


def test_src_has_no_oversized_files():
    assert oversized(ROOT) == [], (
        "Vendored source tarballs are not allowed under src/. Fetch sources "
        "at build time with a pinned URL and sha256."
    )


def test_checker_flags_a_large_file(tmp_path):
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True)  # noqa: E731
    run("init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "big.tar.xz").write_bytes(b"0" * (LIMIT_BYTES + 1))
    (tmp_path / "src" / "ok.spec").write_text("x")
    run("add", "-A")
    assert [n for n, _ in oversized(tmp_path)] == ["src/big.tar.xz"]
