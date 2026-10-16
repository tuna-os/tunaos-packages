"""Read a Portage binhost `Packages` index; compare Gentoo versions.

The gpkg member of the format-neutral index layer (scripts/repo_index.py):
`parse_packages` returns the standard index shape and `iter_rows` keeps
every entry, so the hygiene and regression checks read the gentoo binhost
the way they read rpm-md, apt and pacman repositories (ADR 0002).

A `Packages` file is a header stanza (ARCH, VERSION, ...) followed by one
stanza per binary package: `CPV`, `RDEPEND`, `PATH`, `BUILD_ID`, ...
Packages are named `category/name`, because a bare name is ambiguous in
Gentoo.

Versions follow PMS section 3.3, not any other format's ordering:
`1.0_rc1 < 1.0 < 1.0_p1`, a component with a leading zero compares as a
string (`1.01 < 1.1`), and `-rN` is a revision.
"""
from __future__ import annotations

import re

SUFFIX_ORDER = {"alpha": 0, "beta": 1, "pre": 2, "rc": 3, "p": 5}
# The "no further suffix" position: below _p, above every prerelease.
_END = 4
VERSION = re.compile(
    r"(?P<numbers>[0-9]+(?:\.[0-9]+)*)(?P<letter>[a-z])?"
    r"(?P<suffixes>(?:_(?:alpha|beta|pre|rc|p)[0-9]*)*)(?:-r(?P<revision>[0-9]+))?"
)
ATOM = re.compile(r"^(?P<op>>=|<=|=|~|<|>)?(?P<body>[^\s\[:]+)(?::[^\s\[]*)?(?:\[.*\])?$")
NAME_VERSION = re.compile(r"^(?P<name>.+?)-(?P<version>[0-9][^-]*(?:-r[0-9]+)?)$")


def parse_version(version: str) -> tuple:
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError(f"not a Gentoo version: {version!r}")
    suffixes = [
        (SUFFIX_ORDER[kind], int(number or 0))
        for kind, number in re.findall(r"_(alpha|beta|pre|rc|p)([0-9]*)", match["suffixes"])
    ]
    return (match["numbers"].split("."), match["letter"] or "", suffixes,
            int(match["revision"] or 0))


def _compare_components(left: list[str], right: list[str]) -> int:
    if int(left[0]) != int(right[0]):
        return -1 if int(left[0]) < int(right[0]) else 1
    for a, b in zip(left[1:], right[1:]):
        if a.startswith("0") or b.startswith("0"):
            a, b = a.rstrip("0"), b.rstrip("0")
        else:
            a, b = int(a), int(b)
        if a != b:
            return -1 if a < b else 1
    return (len(left) > len(right)) - (len(left) < len(right))


def compare(left: str, right: str, *, revision: bool = True) -> int:
    """PMS 3.3 ordering: -1, 0 or 1."""
    a, b = parse_version(left), parse_version(right)
    result = _compare_components(a[0], b[0])
    if result:
        return result
    if a[1] != b[1]:
        return -1 if a[1] < b[1] else 1
    for index in range(max(len(a[2]), len(b[2]))):
        x = a[2][index] if index < len(a[2]) else (_END, 0)
        y = b[2][index] if index < len(b[2]) else (_END, 0)
        if x != y:
            return -1 if x < y else 1
    if revision and a[3] != b[3]:
        return -1 if a[3] < b[3] else 1
    return 0


def satisfies(available: str, op: str, required: str) -> bool:
    """Does `available` meet `<op><name>-<required>`? `=*` is a prefix match."""
    if op == "=*":
        if available == required:
            return True
        return available.startswith(required) and not available[len(required)].isdigit()
    if op == "~":
        return compare(available, required, revision=False) == 0
    result = compare(available, required)
    return {
        ">=": result >= 0, ">": result > 0, "<=": result <= 0,
        "<": result < 0, "=": result == 0,
    }[op]


def stanzas(text: str) -> list[dict[str, str]]:
    blocks = []
    for block in re.split(r"\n\s*\n", text.strip()):
        fields = {}
        for line in block.splitlines():
            key, _, value = line.partition(":")
            if _:
                fields[key.strip()] = value.strip()
        if fields:
            blocks.append(fields)
    return blocks


def split_cpv(cpv: str) -> tuple[str, str]:
    match = NAME_VERSION.match(cpv)
    if not match:
        raise ValueError(f"not a CPV: {cpv!r}")
    return match["name"], match["version"]


def dependencies(rdepend: str) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Plain and versioned package requirements of a dependency string.

    `|| ( ... )` groups are skipped entirely: any branch may satisfy one, and
    a gate that cannot judge leans lenient, as the deb reader does. Blockers
    are not requirements. USE conditionals are already reduced in a binhost
    index; a stray `flag?` token is ignored and its group read as plain.
    """
    plain: list[str] = []
    versioned: list[tuple[str, str, str]] = []
    tokens = rdepend.split()
    depth_skip = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if depth_skip:
            depth_skip += token == "("
            depth_skip -= token == ")"
            continue
        if token == "||":
            depth_skip = 0
            if index < len(tokens) and tokens[index] == "(":
                index += 1
                depth_skip = 1
            continue
        if token in {"(", ")"} or token.endswith("?") or token.startswith("!"):
            continue
        match = ATOM.match(token)
        if not match or "/" not in match["body"]:
            continue
        body = match["body"]
        if not match["op"]:
            plain.append(body)
            continue
        op = match["op"]
        if body.endswith("*"):
            body, op = body[:-1], "=*"
        try:
            name, version = split_cpv(body)
        except ValueError:
            continue
        versioned.append((name, op, version))
        plain.append(name)
    return plain, versioned


def parse_packages(text: str) -> dict:
    """The standard index shape, keyed by `category/name`.

    With several builds of one name, the highest version is the one an
    emerge would pick, so it is the one recorded.
    """
    blocks = stanzas(text)
    arch = blocks[0].get("ARCH", "") if blocks and "CPV" not in blocks[0] else ""
    index: dict = {"packages": {}, "provides": {}, "provides_evr": {}, "files": set()}
    for block in blocks:
        if "CPV" not in block:
            continue
        name, version = split_cpv(block["CPV"])
        held = index["packages"].get(name)
        if held is not None and compare(version, held["evr"]) <= 0:
            continue
        requires, requires_versioned = dependencies(block.get("RDEPEND", ""))
        index["packages"][name] = {
            "arch": arch, "evr": version, "srpm": name,
            "requires": requires, "requires_versioned": requires_versioned,
            "files": [],
        }
        index["provides"][name] = {name}
        index["provides_evr"][name] = {version}
    return index


def iter_rows(text: str):
    """Every package stanza as a hygiene row, duplicates preserved."""
    blocks = stanzas(text)
    arch = blocks[0].get("ARCH", "") if blocks and "CPV" not in blocks[0] else ""
    for block in blocks:
        if "CPV" not in block:
            continue
        name, version = split_cpv(block["CPV"])
        yield {
            "name": name,
            "arch": arch,
            "evr": version,
            "srpm": name,
            "location": block.get("PATH") or f"{block['CPV']}.gpkg.tar",
            "files": [],
        }
