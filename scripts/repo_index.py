"""The format-neutral index layer: one way to read any served repository.

`manifests/package-factory.yaml` declares three package formats — rpm,
deb, pkg.tar.zst — and every one of them is a first-class target. Yet
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
deb_version for deb, pacman_db for pkg.tar.zst. Judging a Debian
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

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apt_packages  # noqa: E402
import deb_version  # noqa: E402
import gap_engine  # noqa: E402
import pacman_db  # noqa: E402
import rpm_vercmp  # noqa: E402

FORMATS = ("rpm", "deb", "pkg.tar.zst")


def version_module(fmt: str):
    """The comparator that is CORRECT for this format's versions."""
    if fmt == "rpm":
        return rpm_vercmp
    if fmt == "deb":
        return deb_version
    if fmt == "pkg.tar.zst":
        return pacman_db
    raise ValueError(f"no version comparator for format {fmt!r}")


def satisfies(fmt: str, available: str, op: str, required: str) -> bool:
    return version_module(fmt).satisfies(available, op, required)


def fetch(url: str, cache: pathlib.Path) -> bytes:
    """Cached HTTPS fetch with the factory's User-Agent (rpm engine's)."""
    return gap_engine.fetch(url, cache)


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
        return gap_engine.parse_primary(gap_engine.primary_of(url, cache)[0])
    if fmt == "deb":
        name, blob = _fetch_first(
            url, ["Packages.gz", "Packages.xz", "Packages"], cache)
        text = apt_packages.decompress(name, blob).decode("utf-8", "replace")
        return apt_packages.parse_packages(text)
    if fmt == "pkg.tar.zst":
        db = f"{repo_name}.db" if repo_name else "tunaos.db"
        _, blob = _fetch_first(url, [db], cache)
        return pacman_db.parse_db(blob)
    raise ValueError(f"no index reader for format {fmt!r}")


def iter_rows(url: str, fmt: str, cache: pathlib.Path,
              repo_name: str | None = None):
    """Every index entry as a hygiene row, duplicates preserved.

    parse-style readers key by name and cannot see duplicate entries —
    the exact defect the hygiene duplicate checks exist for — so this
    path keeps every entry.
    """
    if fmt == "rpm":
        yield from iter_rpm_rows(gap_engine.primary_of(url, cache)[0])
        return
    if fmt == "deb":
        name, blob = _fetch_first(
            url, ["Packages.gz", "Packages.xz", "Packages"], cache)
        text = apt_packages.decompress(name, blob).decode("utf-8", "replace")
        yield from apt_packages.iter_rows(text)
        return
    if fmt == "pkg.tar.zst":
        db = f"{repo_name}.db" if repo_name else "tunaos.db"
        _, blob = _fetch_first(url, [db], cache)
        yield from pacman_db.iter_rows(blob)
        return
    raise ValueError(f"no index reader for format {fmt!r}")


def iter_rpm_rows(blob: bytes):
    """Every package row of a primary.xml, duplicates included."""
    for _, element in ET.iterparse(io.BytesIO(blob), events=("end",)):
        if element.tag != f"{gap_engine.COMMON}package":
            continue
        version = element.find(f"{gap_engine.COMMON}version")
        source = element.find(
            f"{gap_engine.COMMON}format/{gap_engine.RPM}sourcerpm")
        where = element.find(f"{gap_engine.COMMON}location")
        yield {
            "name": element.findtext(f"{gap_engine.COMMON}name"),
            "arch": element.findtext(f"{gap_engine.COMMON}arch"),
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
                      element.findall(
                          f"{gap_engine.COMMON}format/{gap_engine.COMMON}file")
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
