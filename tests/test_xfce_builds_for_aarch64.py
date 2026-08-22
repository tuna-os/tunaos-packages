"""el10/aarch64 must have an xfce build-chain cell.

Measured live on 2026-08-22: el10/x86_64 served 91 of 130 catalog entries and
el10/aarch64 served 20, and 88 of the difference were build-chain products --
xfce4-panel, xfce4-session, xfce4-settings, xfce4-terminal, thunar, xfdesktop,
xfwl4, garcon, exo, libxfce4ui, libxfce4util, tumbler and the gnome set.
`xfce/10-stream-aarch64/repodata/repomd.xml` returned 404 while the x86_64
prefix served 110 package names.

The cause was not a failing cell: `plan-package-factory.py --selector
engine=build-chain` emitted 8 cells and only fprintd and hummingbird had an
aarch64 entry at all (#480). XFCE could not be assembled on el10 aarch64
because none of its packages were ever built there.

Nothing structurally blocked it — this is a cell definition, not a port.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILDS = ROOT / "manifests" / "package-builds.yaml"


def cells() -> dict:
    spec = yaml.safe_load(BUILDS.read_text(encoding="utf-8"))
    entries = spec["builds"] if "builds" in spec else next(v for v in spec.values() if isinstance(v, list))
    return {c["id"]: c for c in entries}


def test_the_aarch64_xfce_cell_exists():
    assert "xfce-el10-aarch64" in cells()


def test_it_runs_on_a_native_arm_runner():
    """Emulated aarch64 mock is unusably slow; fprintd-el10-aarch64 and
    hummingbird-aarch64 both take the native runner."""
    c = cells()["xfce-el10-aarch64"]
    assert c["runner"] == "ubuntu-24.04-arm"
    assert c["architecture"] == "aarch64"


def test_it_uses_the_aarch64_mock_target_and_image():
    """centos-stream-10-ci is an x86_64 chroot. Pointing an aarch64 cell at it
    is the mistake that would silently produce x86_64 packages or fail deep in
    the build."""
    c = cells()["xfce-el10-aarch64"]
    assert c["mock_config"] == "centos-stream-10-ci-aarch64"
    assert c["image"].endswith("-aarch64")


def test_that_mock_config_actually_exists():
    assert (ROOT / "mock" / "centos-stream-10-ci-aarch64.cfg").is_file()


def test_it_publishes_to_an_arch_specific_prefix():
    """Sharing the x86_64 prefix would let two arches rclone sync over each
    other -- the #124 repo-wipe shape."""
    c = cells()["xfce-el10-aarch64"]
    assert c["r2_path"] == "xfce/10-stream-aarch64"
    assert c["r2_path"] != cells()["xfce-el10-x86_64"]["r2_path"]


def test_it_shares_the_x86_64_build_order():
    """The cell supplies mock_config and r2_path, and the runner passes
    --mock-config through, so the manifest's own target/r2_path are not what
    the build uses. A -aarch64 copy would be two files to keep in step for no
    behavioural difference -- hummingbird already shares one."""
    assert cells()["xfce-el10-aarch64"]["manifest"] == cells()["xfce-el10-x86_64"]["manifest"]


def test_the_specs_carry_no_arch_restriction():
    """An ExclusiveArch or %ifarch anywhere under src/xfce-wayland/ would make
    this cell fail rather than build, and would need handling first."""
    hits = subprocess.run(
        ["grep", "-rniE", "ExclusiveArch|ExcludeArch|%ifarch|%ifnarch", "src/xfce-wayland/"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert hits.returncode == 1, hits.stdout


def test_the_planner_emits_it():
    out = subprocess.run(
        ["python3", "scripts/plan-package-factory.py", "--selector", "engine=build-chain",
         "--github-output", "/dev/null"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    ids = {c["id"] for m in json.loads(out.stdout)["matrices"] for c in json.loads(m)["include"]}
    assert "xfce-el10-aarch64" in ids


def test_el10_does_not_yet_claim_an_index_that_does_not_exist():
    """The target contract's rule is to declare only an index that resolves.
    xfce/10-stream-aarch64 does not exist until a publish wave writes it, so
    declaring it now would make factory-status read a 404 as an empty repo."""
    factory = yaml.safe_load((ROOT / "manifests" / "package-factory.yaml").read_text(encoding="utf-8"))
    declared = factory["targets"]["el10"]["published_index"]["aarch64"]
    declared = [declared] if isinstance(declared, str) else declared
    assert not [u for u in declared if "aarch64" in u and "xfce" in u], declared


def test_the_fedora_aarch64_cell_exists_too():
    """xfce had no aarch64 cell on ANY target. fedora is the cheap one: its
    chain builds three packages against stock Fedora, because Fedora 44 ships
    XFCE 4.20 and greetd/gtkgreet/cage are in Fedora proper — so it carries
    neither of the Requires that cannot resolve on EL10 (#482)."""
    assert "xfce-fedora-aarch64" in cells()


def test_the_fedora_aarch64_cell_uses_its_own_mock_config():
    c = cells()["xfce-fedora-aarch64"]
    assert c["mock_config"] == "fedora-44-ci-aarch64"
    assert c["runner"] == "ubuntu-24.04-arm"
    assert (ROOT / "mock" / "fedora-44-ci-aarch64.cfg").is_file()


def test_the_fedora_mock_config_includes_the_aarch64_base_chroot():
    """Including fedora-44-x86_64.cfg would build x86_64 packages in a job
    labelled aarch64 — the failure mode that looks green."""
    text = (ROOT / "mock" / "fedora-44-ci-aarch64.cfg").read_text(encoding="utf-8")
    assert "include('/etc/mock/fedora-44-aarch64.cfg')" in text
    assert "fedora-44-x86_64.cfg" not in text


def test_the_two_fedora_configs_keep_the_same_mock_pkgid_workaround():
    """mock <= 6.7 dies on Fedora 44's rpm 6 at 'unknown tag: pkgid'. If one
    config carries the workaround and the other does not, one arch builds and
    the other fails before compiling anything."""
    for name in ("fedora-44-ci.cfg", "fedora-44-ci-aarch64.cfg"):
        text = (ROOT / "mock" / name).read_text(encoding="utf-8")
        assert "package_state_enable" in text, name


def test_the_fedora_arches_publish_to_distinct_prefixes():
    assert cells()["xfce-fedora-aarch64"]["r2_path"] != cells()["xfce-fedora-x86_64"]["r2_path"]
