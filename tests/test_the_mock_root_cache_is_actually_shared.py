"""Every package in a chain must reuse one mock root cache, not rebuild it.

docs/hummingbird-throughput.md Finding 2 counted the cost across five real
runs: `Start: creating root cache` once per package, `unpacking root cache`
zero times, and the 43 s floor paid 194 times -- 2.32 h of 6.80 h, 34.1% of
all mock time spent rebuilding the same minimal buildroot.

build-chain.sh has been able to fix that since #277, and the fix went inert
without a word: it only mounts the cache when MOCK_CACHE_DIR is set, and the
workflow that set it was removed with the rest of the hummingbird-specific
pipeline (6d4b77a, #517).  package-factory-cell.yml, which replaced it, never
set it.  Nothing failed -- an unshared root cache is merely slow -- which is
exactly why it needs a test rather than a comment.

The failure modes pinned here are all silent ones:

  * an unset MOCK_CACHE_DIR: no mount, no sharing, 34% back on the clock;
  * a mount at /var/cache/mock rather than <config>/root_cache: that also
    shares yum_cache, which accumulates every BuildRequires RPM the chain
    downloads.  The chain now runs 4.5 h and its partial output is what the
    continuation shards resume from, so filling the disk costs a night;
  * a mount path built from anything but the config's own `root`: mock keys
    the cache on config_opts['root'] (buildroot.py `shared_root_name`), so a
    mismatch mounts a directory mock never looks at and says nothing.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAIN = ROOT / "scripts/lib/build-chain/podman.sh"
CELL_WORKFLOW = ROOT / ".github" / "workflows" / "package-factory-cell.yml"


def uncommented(path: Path) -> str:
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_every_mock_profile_names_its_root_after_its_file() -> None:
    """build-chain.sh derives the cache path from MOCK_CONFIG, which is the
    profile's FILENAME.  mock derives it from config_opts['root'].  They are
    the same string in every profile here, and that is the only reason the
    derivation is sound -- so it is pinned rather than assumed."""
    profiles = sorted((ROOT / "mock").glob("*.cfg"))
    assert profiles, "no mock profiles found"
    for cfg in profiles:
        m = re.search(r"""config_opts\['root'\]\s*=\s*['"]([^'"]+)['"]""",
                      cfg.read_text(encoding="utf-8"))
        assert m, f"{cfg.name} does not set config_opts['root']"
        assert m.group(1) == cfg.stem, (
            f"{cfg.name} sets root={m.group(1)!r}; build-chain.sh mounts the "
            f"root cache under MOCK_CONFIG ({cfg.stem!r}), so mock would look "
            "for it somewhere else and silently rebuild it every package"
        )


def test_the_cache_is_mounted_at_the_root_cache_directory() -> None:
    code = uncommented(CHAIN)
    mounts = [line for line in code.splitlines() if "MOCK_CACHE_DIR" in line and "-v " in line]
    assert mounts, "MOCK_CACHE_DIR is no longer mounted into the build container"
    for line in mounts:
        assert "/root_cache:" in line, (
            "the mount target must be <config>/root_cache. Mounting all of "
            "/var/cache/mock also shares yum_cache, which grows without bound "
            f"across a desktop closure: {line.strip()}"
        )
        assert "${MOCK_CONFIG}" in line, (
            "the cache path must come from the config's own name, which is "
            f"what mock keys it on: {line.strip()}"
        )


def test_the_factory_cell_actually_sets_the_cache_dir() -> None:
    text = CELL_WORKFLOW.read_text(encoding="utf-8")
    m = re.search(r"^\s*MOCK_CACHE_DIR:\s*(.+?)\s*$", text, re.M)
    assert m, (
        "package-factory-cell.yml does not set MOCK_CACHE_DIR, so build-chain "
        "mounts nothing and every package rebuilds the buildroot from scratch "
        "-- 34.1% of mock time, measured"
    )
    value = m.group(1)
    assert "runner.temp" in value, (
        "the cache belongs outside the workspace and outside OUT_DIR, which is "
        f"uploaded and published: {value}"
    )
