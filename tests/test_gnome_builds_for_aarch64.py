"""GNOME 50 and 51 must be buildable on aarch64, not only on x86_64.

#480 measured the gap from the served indexes: el10 published 91 package names
on x86_64 against 20 on aarch64, and the reason was not a failing cell but the
absence of one. Four build-chain families had no aarch64 cell at all. xfce
(el10 and fedora) has since been closed; these are the last two.

A desktop that exists on one arch and not the other is exactly what a
consistent multi-arch image set cannot tolerate — an aarch64 image claiming
GNOME is either not being built or is resolving it from somewhere other than
this factory.

Two things this must not get wrong:

  manifest    build-order.yml and build-order-gnome51.yml stay shared across
              arches. Their `target:` feeds only the %{dist} derivation, which
              is .el10 for centos-stream-10-* on either arch, and their
              `r2_path:` is overridden by the cell's. xfce-el10-aarch64 and
              hummingbird-aarch64 already work this way; only fprintd has an
              arch-specific manifest, and that is because its aarch64 leg
              builds genuinely different content.

  r2 prefix   gnome50's x86_64 leg publishes into repo/10-x86_64, the shared
              pre-existing COPR-mirror prefix that el10 tideforge packages also
              merge into. There is no aarch64 equivalent, and a new leg must
              not write into a prefix it does not own, so aarch64 gets a fresh
              family-owned path.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifests" / "package-builds.yaml"
FACTORY = ROOT / "manifests" / "package-factory.yaml"
NEW = ("gnome50-el10-aarch64", "gnome51-el10-aarch64")


def cells() -> dict[str, dict]:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return {cell["id"]: cell for cell in manifest["native_builds"]}


def planned(*args: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "plan-package-factory.py"), *args],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    return [cell for m in payload["matrices"] for cell in json.loads(m)["include"]]


def test_the_cells_exist():
    defined = cells()
    for cell_id in NEW:
        assert cell_id in defined, cell_id


def test_every_build_chain_family_now_has_both_arches():
    """The parity assertion #480 actually asks for, stated over the planner so
    it covers families added later too."""
    by_family: dict[str, set[str]] = {}
    for cell in planned("--selector", "engine=build-chain"):
        by_family.setdefault(cell["family"], set()).add(cell["architecture"])
    assert by_family, "no build-chain cells planned"
    missing = {f: a for f, a in by_family.items() if a != {"x86_64", "aarch64"}}
    assert not missing, missing


def test_each_is_dispatchable_by_id():
    for cell_id in NEW:
        assert [c["id"] for c in planned("--cell", cell_id)] == [cell_id]


def test_they_run_on_a_native_arm_runner():
    """Cross-building under qemu would take the 180-minute timeout and prove
    nothing about the arch the packages actually run on."""
    for cell_id in NEW:
        cell = cells()[cell_id]
        assert cell["runner"] == "ubuntu-24.04-arm", cell_id
        assert cell["image"].endswith("-aarch64"), cell_id


def test_they_reuse_the_x86_64_manifest():
    """Shared, not forked. A second copy of an 85-spec build order is a second
    place to forget a package."""
    defined = cells()
    for series in ("50", "51"):
        x86 = defined[f"gnome{series}-el10-x86_64"]
        arm = defined[f"gnome{series}-el10-aarch64"]
        assert arm["manifest"] == x86["manifest"], series
        assert arm["source_paths"] == x86["source_paths"], series


def test_the_aarch64_leg_gets_its_own_r2_prefix():
    """Never the x86_64 one: two arches writing one flat prefix means each
    publish `rclone sync` deletes the other's packages (#124)."""
    defined = cells()
    prefixes = [cell["r2_path"] for cell in defined.values() if cell.get("r2_path")]
    assert len(prefixes) == len(set(prefixes)), "two cells share an r2_path"
    for cell_id in NEW:
        assert defined[cell_id]["r2_path"].endswith("aarch64"), cell_id


def test_gnome50_aarch64_does_not_inherit_the_legacy_shared_prefix():
    """repo/10-x86_64 is the pre-existing COPR-mirror prefix that el10
    tideforge packages merge into as well. A new leg must own its path."""
    defined = cells()
    assert defined["gnome50-el10-x86_64"]["r2_path"] == "repo/10-x86_64"
    assert defined["gnome50-el10-aarch64"]["r2_path"] == "gnome50/10-stream-aarch64"


def test_the_mock_configs_exist_and_target_aarch64():
    for cell_id in NEW:
        config = ROOT / "mock" / f"{cells()[cell_id]['mock_config']}.cfg"
        assert config.is_file(), config
        text = config.read_text(encoding="utf-8")
        ast.parse(text)  # mock configs are executed as Python
        assert "epel-10-aarch64.cfg" in text, config
        # Comments legitimately explain what was swapped FROM; the executable
        # lines must not still point at an x86_64 URL or chroot template.
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert "x86_64" not in code, f"{config} still names x86_64 in code"


def test_the_gnome51_chroot_root_matches_its_config_name():
    """mock keys its chroot cache on root. A config whose root collides with
    another family's would have them share a cache."""
    roots = {}
    for config in sorted((ROOT / "mock").glob("*.cfg")):
        for line in config.read_text(encoding="utf-8").splitlines():
            if line.startswith("config_opts['root']"):
                roots.setdefault(line.split("=", 1)[1].strip().strip("'\""), []).append(config.name)
    assert roots["centos-stream-10-ci-gnome51-aarch64"] == [
        "centos-stream-10-ci-gnome51-aarch64.cfg"
    ]


def test_the_new_prefixes_are_not_yet_declared_as_published():
    """manifests/package-factory.yaml's own rule: a prefix joins
    published_index only once it resolves. These 404 until the first wave."""
    factory = yaml.safe_load(FACTORY.read_text(encoding="utf-8"))
    declared = factory["targets"]["el10"]["published_index"]["aarch64"]
    declared = [declared] if isinstance(declared, str) else declared
    for cell_id in NEW:
        prefix = cells()[cell_id]["r2_path"]
        assert not any(prefix in url for url in declared), prefix
