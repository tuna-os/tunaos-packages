"""Reader for a pacman repository database, plus libalpm's vercmp.

The arch target publishes with repo-add (scripts/publish-arch-wave.sh):
a `<name>.db` tar archive of `<pkg>-<ver>-<rel>/desc` entries. This
module parses that into the same row/index structures the RPM and deb
checks consume, so the moment the arch target declares a
`published_index` the format-neutral checks cover it too — the factory
contract lists pkg.tar.zst as a first-class format, and tooling that
cannot read it re-creates the EL-centric bias RFC 011 exists to remove.

`vercmp` is libalpm's algorithm. It descends from an early rpmvercmp
but is NOT today's RPM comparison: alpm has no `~`/`^` handling, and a
trailing alphabetic segment sorts OLDER than nothing (`1.0a < 1.0`,
`1.0rc1 < 1.0`) where RPM sorts it newer. Epoch is `epoch:`, pkgrel is
the suffix after the last `-`, compared only when both sides carry one
— pacman's own wrapper semantics.
"""
from __future__ import annotations

import io
import re
import tarfile

_OP = re.compile(r"^(?P<name>[^<>=]+?)(?:(?P<op><=|>=|=|<|>)(?P<version>.+))?$")


def _segments(version: str):
    """Alternating alnum runs, alpm-style: split on non-alnum, then on
    the digit/alpha boundary."""
    for block in re.split(r"[^A-Za-z0-9]+", version):
        yield from re.findall(r"\d+|[A-Za-z]+", block)


def _cmp_version(a: str, b: str) -> int:
    sa, sb = list(_segments(a)), list(_segments(b))
    for seg_a, seg_b in zip(sa, sb):
        a_num, b_num = seg_a.isdigit(), seg_b.isdigit()
        if a_num and b_num:
            na, nb = int(seg_a), int(seg_b)
            if na != nb:
                return 1 if na > nb else -1
        elif a_num != b_num:
            return 1 if a_num else -1
        elif seg_a != seg_b:
            return 1 if seg_a > seg_b else -1
    if len(sa) == len(sb):
        return 0
    # The alpm tail rule: a remaining ALPHA segment never beats an
    # empty string ("1.0a" < "1.0"), a remaining numeric one does.
    remainder = sa[len(sb):] if len(sa) > len(sb) else sb[len(sa):]
    longer_is_a = len(sa) > len(sb)
    older = remainder[0].isalpha()
    if longer_is_a:
        return -1 if older else 1
    return 1 if older else -1


def vercmp(a: str, b: str) -> int:
    """pacman's vercmp(8): epoch, pkgver, then pkgrel if both have one."""
    a_epoch, a_rest = a.split(":", 1) if ":" in a else ("0", a)
    b_epoch, b_rest = b.split(":", 1) if ":" in b else ("0", b)
    if int(a_epoch or 0) != int(b_epoch or 0):
        return 1 if int(a_epoch or 0) > int(b_epoch or 0) else -1
    a_ver, _, a_rel = a_rest.rpartition("-") if "-" in a_rest else (a_rest, "", "")
    b_ver, _, b_rel = b_rest.rpartition("-") if "-" in b_rest else (b_rest, "", "")
    order = _cmp_version(a_ver or a_rest, b_ver or b_rest)
    if order:
        return order
    if a_rel and b_rel:
        return _cmp_version(a_rel, b_rel)
    return 0


def satisfies(available: str, op: str, required: str) -> bool:
    order = vercmp(available, required)
    if op == "=":
        return order == 0
    if op == ">=":
        return order >= 0
    if op == "<=":
        return order <= 0
    if op == ">":
        return order > 0
    if op == "<":
        return order < 0
    raise ValueError(f"unknown dependency operator: {op!r}")


def parse_dep(text: str) -> tuple[str, str | None, str | None]:
    """`glibc>=2.39` -> (glibc, >=, 2.39); bare names pass through."""
    match = _OP.match(text.strip())
    if not match:
        return text.strip(), None, None
    return match.group("name"), match.group("op"), match.group("version")


def _desc_fields(text: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("%") and line.endswith("%"):
            current = line.strip("%")
            fields[current] = []
        elif line and current:
            fields[current].append(line)
    return fields


def parse_db(blob: bytes) -> dict:
    """A repo-add .db archive in the shape the checks consume."""
    packages: dict[str, dict] = {}
    provides: dict[str, set] = {}
    provides_evr: dict[str, set] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
        for member in archive.getmembers():
            if not member.name.endswith("/desc"):
                continue
            fields = _desc_fields(
                archive.extractfile(member).read().decode("utf-8", "replace"))
            name = (fields.get("NAME") or [""])[0]
            if not name:
                continue
            version = (fields.get("VERSION") or [""])[0]
            deps = [parse_dep(d) for d in fields.get("DEPENDS", [])]
            packages[name] = {
                "arch": (fields.get("ARCH") or [""])[0],
                "evr": version,
                "srpm": (fields.get("BASE") or [name])[0],
                "requires": [dep for dep, _, _ in deps],
                "requires_versioned": [
                    (dep, op, ver) for dep, op, ver in deps if op and ver],
                "files": [],
            }
            provides.setdefault(name, set()).add(name)
            provides_evr.setdefault(name, set()).add(version)
            for entry in fields.get("PROVIDES", []):
                cap, op, ver = parse_dep(entry)
                provides.setdefault(cap, set()).add(name)
                if op == "=" and ver:
                    provides_evr.setdefault(cap, set()).add(ver)
    return {"packages": packages, "provides": provides,
            "provides_evr": provides_evr, "files": set()}


def iter_rows(blob: bytes):
    """Every db entry as a hygiene row. repo-add keys entries by
    name-version directories, so unlike an rpm-md index the db cannot
    hold two entries for one name — the duplicate checks still run,
    and finding one would mean a corrupted db worth hearing about."""
    parsed = parse_db(blob)
    for name, info in parsed["packages"].items():
        yield {"name": name, "arch": info["arch"], "evr": info["evr"],
               "srpm": info["srpm"], "files": []}
