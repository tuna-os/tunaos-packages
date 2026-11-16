"""A failing build can be diffed against the green one, not excavated.

Adapted from koji-diff (slopfest/sandogasa). #480's libnotify diagnosis
reconstructed the buildroot's resolution from issue comments; mock had
already written it into root.log. These pin the extraction, the diff,
and the wiring that keeps the manifests where a later run can reach
them.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract = load("extract_buildroot", "extract-buildroot-manifest.py")
differ = load("diff_buildroots", "diff-buildroots.py")


ROOT_LOG = """\
DEBUG util.py:446:  Package                Arch     Version          Repository  Size
DEBUG util.py:446:  Installing:
DEBUG util.py:446:   gcc                   x86_64   14.2.1-1.el10    appstream   37 M
DEBUG util.py:446:  Installing dependencies:
DEBUG util.py:446:   glibc                 x86_64   2.39-14.el10     baseos      2.2 M
DEBUG util.py:446:   libnotify             x86_64   0.8.6-1.el10     koji        120 k
DEBUG util.py:446:  Installing weak dependencies:
DEBUG util.py:446:   gcc-plugin-annobin    x86_64   14.2.1-1.el10    appstream   58 k
DEBUG util.py:446:  Transaction Summary
DEBUG util.py:446:  ================================
DEBUG util.py:446:  Install  4 Packages
DEBUG util.py:446:  Complete!
"""


def test_the_transaction_tables_become_nevras():
    packages = extract.from_root_log(ROOT_LOG)
    assert packages == {
        "gcc-14.2.1-1.el10.x86_64",
        "glibc-2.39-14.el10.x86_64",
        "libnotify-0.8.6-1.el10.x86_64",
        "gcc-plugin-annobin-14.2.1-1.el10.x86_64",
    }


def test_installed_pkgs_log_wins_over_root_log(tmp_path):
    """mock's own rpm -qa is the finished buildroot, not the plan."""
    (tmp_path / "installed_pkgs.log").write_text(
        "glibc-2.39-14.el10.x86_64\ngcc-14.2.1-1.el10.x86_64\n")
    (tmp_path / "root.log").write_text(ROOT_LOG)
    assert extract.extract(tmp_path) == [
        "gcc-14.2.1-1.el10.x86_64", "glibc-2.39-14.el10.x86_64"]


def test_an_empty_result_is_an_error_not_an_empty_manifest(tmp_path):
    """An empty file would diff as 'everything changed' later."""
    (tmp_path / "root.log").write_text("DEBUG util.py:446:  Complete!\n")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "extract-buildroot-manifest.py"),
         str(tmp_path)], capture_output=True, text=True)
    assert completed.returncode == 1


def test_the_diff_names_what_the_libnotify_hunt_needed():
    """green had 0.8.7 from the published index; red resolved 0.8.6."""
    old = {"libnotify": "0.8.7-1.el10.x86_64", "glibc": "2.39-14.el10.x86_64"}
    new = {"libnotify": "0.8.6-1.el10.x86_64", "glibc": "2.39-14.el10.x86_64",
           "annobin": "12.0-1.el10.x86_64"}
    delta = differ.diff(old, new)
    assert delta == {"added": ["annobin"], "removed": [],
                     "changed": ["libnotify"]}


def test_nevra_parsing_survives_dashes_in_names():
    assert differ.parse_nevra("gtk-layer-shell-0.9.0-1.el10.x86_64") == (
        "gtk-layer-shell", "0.9.0-1.el10.x86_64")


def test_the_chain_records_manifests_only_when_asked():
    """Opt-in by env var, non-fatal on failure, wired in both mock backends."""
    chain = "\n".join(
        (ROOT / path).read_text()
        for path in (
            "scripts/build-chain.sh",
            "scripts/lib/build-chain/podman.sh",
            "scripts/lib/build-chain/mock.sh",
        )
    )
    assert chain.count('record_buildroot_manifest "$resultdir" "$pkg_name"') == 2
    assert 'BUILDROOT_MANIFESTS:-' in chain, "must be opt-in"
    assert "non-fatal" in chain


def test_the_cell_keeps_manifests_inside_the_cached_artifacts():
    """artifacts/buildroots rides the action cache and the success upload.

    Outside artifacts/ the green run's manifests are discarded, and a
    diff needs exactly the run that did NOT fail.
    """
    runner = (ROOT / "scripts" / "run-package-factory-cell.sh").read_text()
    assert 'BUILDROOT_MANIFESTS="$out/artifacts/buildroots"' in runner


def test_nevra_parsing_handles_the_deb_convention_too():
    """`libc6_2.39-0ubuntu8_amd64`: one differ for every chain."""
    assert differ.parse_nevra("libc6_2.39-0ubuntu8_amd64") == (
        "libc6", "2.39-0ubuntu8_amd64")
    delta = differ.diff({"libc6": "2.39-0ubuntu8_amd64"},
                        {"libc6": "2.40-1ubuntu1_amd64"})
    assert delta["changed"] == ["libc6"]


def test_the_deb_chain_records_manifests_like_the_rpm_chain():
    """Parity pin: both chains keep a diffable buildroot record.

    The deb leg snapshots dpkg after build-dep, never fatally, and the
    workflow uploads buildroots/ next to artifacts/ and logs/ — a green
    run's state must survive for the red run's diff.
    """
    chain = (ROOT / "scripts" / "build-deb-chain.sh").read_text()
    assert "dpkg-query -W" in chain
    assert "buildroots" in chain
    assert "non-fatal" in chain
    flow = (ROOT / ".github" / "workflows" / "backport-deb-chain.yml").read_text()
    assert "/buildroots/" in flow
