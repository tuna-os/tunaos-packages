"""The format-neutral index layer: one way to read any served repository.

`manifests/package-factory.yaml` declares four package formats — rpm,
deb, pkg.tar.zst, gpkg — and every one of them is a first-class target. Yet
the de-facto library for reading repositories grew inside the gap
engine (scripts/gap_engine.py, which was literally named
measure-hummingbird-gap.py until the pipeline was de-hardcoded), and
each new tool that imported it inherited an RPM-shaped view of the
world. That is how a factory becomes EL-focused without anyone
deciding it should be.

This module is the standard surface instead. Every reader returns the
SAME index shape, so the checks built on it are format-blind:

    {"packages": {name: {arch, evr, srpm, requires,
                         requires_versioned, files}},
     "provides": {capability: {provider, ...}},
     "provides_evr": {capability: {evr, ...}},
     "files": set()}

and every format names a version module with the same three calls
(`compare`-equivalent, `satisfies`, parsing) — rpm_vercmp for rpm,
deb_version for deb, pacman_db for pkg.tar.zst, gentoo_binhost for gpkg.
Judging a Debian
constraint with the RPM comparator gives wrong answers on real
versions (tests/test_deb_version.py holds a proven disagreement), so
the dispatch is by declared format, never by assumption.

tests/test_target_tooling_parity.py enforces that every format the
factory contract declares is covered here — adding a format without
its toolkit is a red test, not a silent gap.
"""
from __future__ import annotations

import io
import pathlib
import sys
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from module_loader import load_script  # noqa: E402

FORMATS = ("rpm", "deb", "pkg.tar.zst", "gpkg")


def load(name: str, filename: str | None = None):
    """Import a sibling script once, hyphenated filenames included."""
    return load_script(name, filename)

def version_module(fmt: str):
    """The comparator that is CORRECT for this format's versions."""
    if fmt == "rpm":
        return load("rpm_vercmp")
    if fmt == "deb":
        return load("deb_version")
    if fmt == "pkg.tar.zst":
        return load("pacman_db")
    if fmt == "gpkg":
        return load("gentoo_binhost")
    raise ValueError(f"no version comparator for format {fmt!r}")


def satisfies(fmt: str, available: str, op: str, required: str) -> bool:
    return version_module(fmt).satisfies(available, op, required)


def fetch(url: str, cache: pathlib.Path) -> bytes:
    """Cached HTTPS fetch with the factory's User-Agent (rpm engine's)."""
    return load("gap", "gap_engine.py").fetch(url, cache)


def _fetch_first(base: str, names: list[str], cache: pathlib.Path) -> tuple[str, bytes]:
    last_error = None
    for name in names:
        try:
            return name, fetch(base.rstrip("/") + "/" + name, cache)
        except Exception as error:  # noqa: BLE001 - fall through to next name
            last_error = error
    raise SystemExit(f"{base}: none of {names} readable ({last_error})")


def load_index(url: str, fmt: str, cache: pathlib.Path,
               repo_name: str | None = None) -> dict:
    """Read one served repository into the standard index shape."""
    if fmt == "rpm":
        gap = load("gap", "gap_engine.py")
        return gap.parse_primary(gap.primary_of(url, cache)[0])
    if fmt == "deb":
        apt = load("apt_packages")
        name, blob = _fetch_first(
            url, ["Packages.gz", "Packages.xz", "Packages"], cache)
        return apt.parse_packages(apt.decompress(name, blob).decode(
            "utf-8", "replace"))
    if fmt == "pkg.tar.zst":
        pacman = load("pacman_db")
        db = f"{repo_name}.db" if repo_name else "tunaos.db"
        _, blob = _fetch_first(url, [db], cache)
        return pacman.parse_db(blob)
    if fmt == "gpkg":
        _, blob = _fetch_first(url, ["Packages"], cache)
        return load("gentoo_binhost").parse_packages(blob.decode("utf-8", "replace"))
    raise ValueError(f"no index reader for format {fmt!r}")


def iter_rows(url: str, fmt: str, cache: pathlib.Path,
              repo_name: str | None = None):
    """Every index entry as a hygiene row, duplicates preserved.

    parse-style readers key by name and cannot see duplicate entries —
    the exact defect the hygiene duplicate checks exist for — so this
    path keeps every entry.
    """
    if fmt == "rpm":
        gap = load("gap", "gap_engine.py")
        yield from iter_rpm_rows(gap.primary_of(url, cache)[0])
        return
    if fmt == "deb":
        apt = load("apt_packages")
        name, blob = _fetch_first(
            url, ["Packages.gz", "Packages.xz", "Packages"], cache)
        yield from apt.iter_rows(apt.decompress(name, blob).decode(
            "utf-8", "replace"))
        return
    if fmt == "pkg.tar.zst":
        pacman = load("pacman_db")
        db = f"{repo_name}.db" if repo_name else "tunaos.db"
        _, blob = _fetch_first(url, [db], cache)
        yield from pacman.iter_rows(blob)
        return
    if fmt == "gpkg":
        _, blob = _fetch_first(url, ["Packages"], cache)
        yield from load("gentoo_binhost").iter_rows(blob.decode("utf-8", "replace"))
        return
    raise ValueError(f"no index reader for format {fmt!r}")


def iter_rpm_rows(blob: bytes):
    """Every package row of a primary.xml, duplicates included."""
    gap = load("gap", "gap_engine.py")
    for _, element in ET.iterparse(io.BytesIO(blob), events=("end",)):
        if element.tag != f"{gap.COMMON}package":
            continue
        version = element.find(f"{gap.COMMON}version")
        source = element.find(f"{gap.COMMON}format/{gap.RPM}sourcerpm")
        where = element.find(f"{gap.COMMON}location")
        yield {
            "name": element.findtext(f"{gap.COMMON}name"),
            "arch": element.findtext(f"{gap.COMMON}arch"),
            "evr": (f"{version.get('epoch') or '0'}:"
                    f"{version.get('ver')}-{version.get('rel')}"),
            "srpm": source.text if source is not None else None,
            # Where the file sits relative to the index URL. Nothing in the
            # hygiene checks needs it; a reader browsing the repo does, and
            # deriving it from the NEVRA instead would be a guess -- the
            # publisher renames '+' out of filenames, so the guess is wrong
            # for exactly the packages that already caused an incident.
            "location": where.get("href") if where is not None else None,
            # Regular files only: directories are co-owned by design
            # and ghosts have no content to conflict.
            "files": [shipped.text for shipped in
                      element.findall(f"{gap.COMMON}format/{gap.COMMON}file")
                      if shipped.get("type") not in ("dir", "ghost")],
        }
        element.clear()


def source_name(fmt: str, srpm: str | None) -> str | None:
    """Normalize the source-package reference a row carries.

    rpm rows carry a full `foo-1.2-3.el10.src.rpm`; deb and pacman rows
    already carry the bare source/base name. One helper, so no check
    ever mangles a deb source name with the srpm heuristic.
    """
    if not srpm:
        return None
    if fmt == "rpm" or srpm.endswith(".src.rpm"):
        stem = srpm.removesuffix(".src.rpm")
        return stem.rsplit("-", 2)[0]
    return srpm
