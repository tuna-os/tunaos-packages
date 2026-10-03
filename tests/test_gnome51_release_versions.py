"""GNOME 51 must build stable release tarballs, not development snapshots.

Issue #673 was opened while the EL10 tier still carried alpha/beta versions.
Keep the coordinated release set explicit so a partial update cannot publish a
mixed prerelease stack under the gnome51 OCI tag.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

RELEASE_VERSIONS = {
    "gdm": "51.0",
    "gjs": "1.90.0",
    "glib2": "2.90.0",
    "gnome-control-center": "51.0",
    "gnome-desktop3": "51.0",
    "gnome-initial-setup": "51.0",
    "gnome-session": "51.0",
    "gnome-settings-daemon": "51.0",
    "gnome-shell": "51.0",
    "gsettings-desktop-schemas": "51.0",
    "gtk4": "4.24.0",
    "libadwaita": "1.10.0",
    "mutter": "51.0",
    # GNOME published Nautilus 51.0.1 as the first stable 51 tarball.
    "nautilus": "51.0.1",
    "orca": "51.0",
    "xdg-desktop-portal-gnome": "51.0",
}

SOURCE_TARBALLS = {
    **{package: f"{package}-{version}.tar.xz" for package, version in RELEASE_VERSIONS.items()},
    "glib2": "glib-2.90.0.tar.xz",
    "gnome-desktop3": "gnome-desktop-51.0.tar.xz",
    "gtk4": "gtk-4.24.0.tar.xz",
}


def spec_for(package: str) -> pathlib.Path:
    return ROOT / "src" / "gnome-51" / package / f"{package}.spec"


def version_of(spec: pathlib.Path) -> str:
    match = re.search(r"^Version:\s*(\S+)", spec.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{spec} has no Version field"
    return match.group(1)


@pytest.mark.parametrize("package,expected", RELEASE_VERSIONS.items())
def test_gnome51_component_uses_the_stable_release(package: str, expected: str) -> None:
    spec = spec_for(package)
    assert version_of(spec) == expected, (
        f"{spec.relative_to(ROOT)} is not at the coordinated GNOME 51 release; "
        "do not publish a mixed alpha/beta/final family index"
    )


def test_glib2_bootstrap_matches_the_full_release() -> None:
    bootstrap = ROOT / "src/gnome-51/glib2/glib2-bootstrap.spec"
    assert version_of(bootstrap) == RELEASE_VERSIONS["glib2"]


@pytest.mark.parametrize("package,tarball", SOURCE_TARBALLS.items())
def test_the_sources_checksum_names_the_release_tarball(package: str, tarball: str) -> None:
    sources = spec_for(package).parent / "sources"
    first_line = sources.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith(f"SHA512 ({tarball}) = "), (
        f"{sources.relative_to(ROOT)} does not verify the tarball selected by the spec"
    )


def test_the_release_specs_do_not_encode_prerelease_tarball_names() -> None:
    for package in RELEASE_VERSIONS:
        text = spec_for(package).read_text(encoding="utf-8")
        declarations = "\n".join(
            line for line in text.splitlines()
            if line.startswith(("Version:", "%global tarball_version"))
        )
        assert not re.search(r"(?:alpha|beta|rc)", declarations), (
            f"{package} still selects a prerelease tarball: {declarations}"
        )
