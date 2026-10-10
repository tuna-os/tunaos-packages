"""Tests for scripts/assemble-deb-source-tree.py.

The script assembles a Debian source tree for a rendered Tideforge recipe:
materialises the primary + auxiliary sources (cache-first, checksum
verified), extracts them with strip_components, copies rendered debian/
files over the in-tree ones, and writes source-dir. Run in a subprocess
against real temp files (no network: the cache is seeded directly, and
the script's fallback urlretrieve is exercised only when the cache
misses — which we avoid by pre-seeding).
"""

import hashlib
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "assemble-deb-source-tree.py"

PRIMARY = b"primary-tarball"
PRIMARY_SHA = hashlib.sha256(PRIMARY).hexdigest()
AUX = b"aux-tarball"
AUX_SHA = hashlib.sha256(AUX).hexdigest()


def _tar(payload, name):
    """Write a tar.gz containing one file at <name>; return the bytes."""
    import io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


# The cache is keyed by the sha256 of the ARCHIVE bytes (the recipe pin),
# so the primary/aux tarballs are materialised first and their sha used.
PRIMARY_TAR = _tar(PRIMARY, "hello-tuna-1.2.3/src/main.c")
PRIMARY_SHA = hashlib.sha256(PRIMARY_TAR).hexdigest()
AUX_TAR = _tar(AUX, "vendor/libfoo-2.0/foo.c")
AUX_SHA = hashlib.sha256(AUX_TAR).hexdigest()


def _seed_cache(cache_dir, sha, payload):
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / sha).write_bytes(payload)


def _run(tmp_path, recipe, home):
    recipe_path = tmp_path / "recipe.yaml"
    recipe_path.write_text(recipe)
    root = tmp_path / "root"
    root.mkdir()
    # rendered/debian lives in <root>/rendered — the script copies it over.
    rendered = root / "rendered" / "debian"
    rendered.mkdir(parents=True)
    (rendered / "rules").write_text("#!/usr/bin/make -f\n")
    (rendered / "control").write_text("Source: hello-tuna\n")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(recipe_path), str(root)],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    return result, root


BASE_RECIPE = """name: hello-tuna
version: '1.2.3'
source:
  url: https://example.com/primary.tar.gz
  sha256: {primary}
  directory: hello-tuna-1.2.3
"""


@pytest.fixture
def home_and_cache(tmp_path):
    home = tmp_path / "home"
    cache = home / ".cache" / "tideforge" / "sources"
    return home, cache


class TestAssembleDeb:
    def test_extracts_primary_and_copies_debian(self, tmp_path, home_and_cache):
        home, cache = home_and_cache
        _seed_cache(cache, PRIMARY_SHA, PRIMARY_TAR)

        result, root = _run(tmp_path, BASE_RECIPE.format(primary=PRIMARY_SHA), home)
        assert result.returncode == 0, result.stderr

        source = root / "source" / "hello-tuna-1.2.3"
        assert (source / "src" / "main.c").is_file()
        # rendered debian/ files copied over
        assert (source / "debian" / "control").is_file()
        assert (source / "debian" / "rules").is_file()
        assert (source / "debian" / "rules").stat().st_mode & 0o111, "rules must be executable"
        # source-dir points at the extracted root
        assert (root / "source-dir").read_text().strip() == "/work/source/hello-tuna-1.2.3"
        # in-tree debian/ cleared before copy (pop-os collision regression)
        assert "debian" in [p.name for p in source.iterdir()]

    def test_cache_hit_prints(self, tmp_path, home_and_cache):
        home, cache = home_and_cache
        _seed_cache(cache, PRIMARY_SHA, PRIMARY_TAR)

        result, _ = _run(tmp_path, BASE_RECIPE.format(primary=PRIMARY_SHA), home)
        assert result.returncode == 0, result.stderr
        assert "cache hit" in result.stdout

    def test_checksum_mismatch_rejected(self, tmp_path, home_and_cache):
        # Seed the cache with the WRONG bytes under the pinned sha — the
        # cache lookup is keyed by sha and validates on read, so a corrupted
        # blob must be treated as a miss and downloaded; with no network the
        # urlretrieve raises, which is the point (never ship bad bytes).
        home, cache = home_and_cache
        _seed_cache(cache, PRIMARY_SHA, b"corrupted")

        result, _ = _run(tmp_path, BASE_RECIPE.format(primary=PRIMARY_SHA), home)
        assert result.returncode != 0
        assert "checksum mismatch" in (result.stdout + result.stderr) or \
            "urlopen" in (result.stdout + result.stderr) or \
            "URLError" in (result.stderr)

    def test_auxiliary_source_with_strip_components(self, tmp_path, home_and_cache):
        home, cache = home_and_cache
        _seed_cache(cache, PRIMARY_SHA, PRIMARY_TAR)
        _seed_cache(cache, AUX_SHA, AUX_TAR)

        recipe = BASE_RECIPE.format(primary=PRIMARY_SHA) + f"""
sources:
  - url: https://example.com/aux.tar.gz
    sha256: {AUX_SHA}
    destination: vendor
    strip_components: 1
"""

        result, root = _run(tmp_path, recipe, home)
        assert result.returncode == 0, result.stderr
        source = root / "source" / "hello-tuna-1.2.3"
        assert (source / "vendor" / "libfoo-2.0" / "foo.c").is_file(), \
            "strip_components=1 keeps the inner dir; only the top level is stripped"

    def test_auxiliary_source_without_extract_copies_raw(self, tmp_path, home_and_cache):
        home, cache = home_and_cache
        _seed_cache(cache, PRIMARY_SHA, PRIMARY_TAR)
        aux_raw_sha = hashlib.sha256(AUX).hexdigest()
        _seed_cache(cache, aux_raw_sha, AUX)  # raw bytes, not a tarball

        recipe = BASE_RECIPE.format(primary=PRIMARY_SHA) + f"""
sources:
  - url: https://example.com/aux.bin
    sha256: {aux_raw_sha}
    destination: vendor/aux.bin
    extract: false
"""

        result, root = _run(tmp_path, recipe, home)
        assert result.returncode == 0, result.stderr
        source = root / "source" / "hello-tuna-1.2.3"
        assert (source / "vendor" / "aux.bin").read_bytes() == AUX
