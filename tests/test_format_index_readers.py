"""Every declared package format reads into ONE index shape.

The factory contract declares rpm, deb, and pkg.tar.zst as first-class
formats; a check that can only read rpm-md re-creates the EL-centric
bias RFC 011 removed from the build side. These pin the deb and pacman
readers to the exact artifacts the publishers write (apt-ftparchive
flat Packages; repo-add .db), and the pacman comparator to libalpm's
documented divergences from RPM.
"""
from __future__ import annotations

import importlib.util
import io
import pathlib
import tarfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


apt = load("apt_packages")
pacman = load("pacman_db")
repo_index = load("repo_index")


PACKAGES = """\
Package: quickshell
Version: 0.1.0-1
Architecture: amd64
Depends: libc6 (>= 2.34), libqt6qml6 | libqt6qml6t64, dms-cli
Provides: shell-runtime (= 0.1.0)
Filename: pool/quickshell_0.1.0-1_amd64.deb

Package: dms
Source: dankmaterialshell
Version: 1.2.0-1
Architecture: all
Pre-Depends: quickshell (>= 0.1.0)
Filename: pool/dms_1.2.0-1_all.deb
"""


def test_apt_stanzas_become_the_standard_index_shape():
    index = apt.parse_packages(PACKAGES)
    assert index["packages"]["quickshell"]["evr"] == "0.1.0-1"
    assert index["packages"]["dms"]["srpm"] == "dankmaterialshell"
    assert index["packages"]["quickshell"]["srpm"] == "quickshell"
    assert index["provides"]["shell-runtime"] == {"quickshell"}
    assert index["provides_evr"]["shell-runtime"] == {"0.1.0"}


def test_apt_or_alternatives_never_become_hard_requires():
    """`a | b` must not be judged as a bare require on `a` — either
    branch can satisfy it, and calling it missing invents a blocker."""
    index = apt.parse_packages(PACKAGES)
    quickshell = index["packages"]["quickshell"]
    assert "libqt6qml6" not in quickshell["requires"]
    assert ["libc6", "dms-cli"] == quickshell["requires"]
    assert ("libc6", ">=", "2.34") in quickshell["requires_versioned"]


def test_apt_pre_depends_count_as_depends():
    index = apt.parse_packages(PACKAGES)
    assert ("quickshell", ">=", "0.1.0") in index["packages"]["dms"][
        "requires_versioned"]


def test_apt_rows_keep_source_names_unmangled():
    """`Source: dankmaterialshell` must never go through the srpm
    heuristic, which would strip it to `dankmaterialshell` minus two
    dash-segments on other names."""
    rows = list(apt.iter_rows(PACKAGES))
    assert rows[1]["srpm"] == "dankmaterialshell"
    assert repo_index.source_name("deb", "gtk-layer-shell") == "gtk-layer-shell"
    assert repo_index.source_name(
        "rpm", "gtk-layer-shell-0.9.0-1.el10.src.rpm") == "gtk-layer-shell"


def _db(entries) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for directory, desc in entries:
            data = desc.encode()
            info = tarfile.TarInfo(f"{directory}/desc")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


DESC = """\
%NAME%
niri

%VERSION%
25.02-1

%ARCH%
x86_64

%BASE%
niri

%DEPENDS%
glibc>=2.39
libinput

%PROVIDES%
wayland-compositor=25.02
"""


def test_pacman_db_becomes_the_standard_index_shape():
    index = pacman.parse_db(_db([("niri-25.02-1", DESC)]))
    niri = index["packages"]["niri"]
    assert niri["evr"] == "25.02-1"
    assert niri["requires"] == ["glibc", "libinput"]
    assert ("glibc", ">=", "2.39") in niri["requires_versioned"]
    assert index["provides"]["wayland-compositor"] == {"niri"}


@pytest.mark.parametrize("a,b,expected", [
    ("1.0", "1.0", 0),
    ("1.0.1", "1.0", 1),
    ("1.0a", "1.0", -1),      # alpm: trailing alpha is OLDER — rpm disagrees
    ("1.0rc1", "1.0", -1),
    ("1:0.5", "2.0", 1),      # epoch dominates
    ("1.0-1", "1.0-2", -1),
    ("1.0", "1.0-2", 0),      # pkgrel compared only when both carry one
    ("1.10", "1.9", 1),
])
def test_pacman_vercmp(a, b, expected):
    assert pacman.vercmp(a, b) == expected


def test_pacman_and_rpm_genuinely_disagree():
    """`1.0a` vs `1.0`: alpm says older, rpm says newer. One comparator
    per format, or the arch leg inherits Fedora's answer."""
    rpm = load("rpm_vercmp")
    assert pacman.vercmp("1.0a", "1.0") == -1
    assert rpm.rpmvercmp("1.0a", "1.0") == 1




def test_every_declared_format_has_a_reader_and_a_comparator():
    """The parity floor: FORMATS here must cover the factory contract."""
    import yaml
    contract = yaml.safe_load(
        (ROOT / "manifests" / "package-factory.yaml").read_text())
    declared = {t.get("format") for t in contract["targets"].values()}
    assert declared <= set(repo_index.FORMATS), (
        f"formats {declared - set(repo_index.FORMATS)} declared in the "
        "factory contract have no index reader/comparator in repo_index.py")
