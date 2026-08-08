#!/usr/bin/env python3
"""Measure what a Hummingbird desktop needs that Hummingbird does not ship.

This is a measurement, not an estimate.  It reads two real rpm-md indexes —
the Hummingbird target repository named in the catalog, and a Fedora reference
repository (Rawhide by default, which is what Hummingbird tracks) — and
computes, for each desktop:

  * which of the desktop's install roots Hummingbird already provides,
  * the transitive Requires: closure of the roots in the Fedora reference,
  * the subset of that closure Hummingbird cannot satisfy from any package,
    Provides: or shipped file — i.e. the packages that must be rebuilt,
  * the source packages behind them, ordered into build tiers.

Nothing here is inferred from a name, a release number or a base image string.
Every answer comes out of the two indexes, and --report-json records the
repomd revisions and primary.xml checksums the answer was computed from so a
later run can say whether the inputs moved.

Usage:
    scripts/measure-hummingbird-gap.py --catalog manifests/hummingbird-desktops.yaml
    scripts/measure-hummingbird-gap.py --desktop gnome --report-json gap.json \
        --build-order build-order-hummingbird-desktops.yml
"""
from __future__ import annotations

import argparse
import collections
import datetime
import gzip
import hashlib
import io
import json
import lzma
import pathlib
import sys
import urllib.request
import xml.etree.ElementTree as ET

import yaml

COMMON = "{http://linux.duke.edu/metadata/common}"
RPM = "{http://linux.duke.edu/metadata/rpm}"
REPO = "{http://linux.duke.edu/metadata/repo}"

DEFAULT_REFERENCE = (
    "https://dl.fedoraproject.org/pub/fedora/linux/development/rawhide"
    "/Everything/x86_64/os/"
)
DEFAULT_SOURCE_REFERENCE = (
    "https://dl.fedoraproject.org/pub/fedora/linux/development/rawhide"
    "/Everything/source/tree/"
)

# Capabilities no rebuild can supply because they are the buildroot itself or
# an unversioned rich-dep alternative that rpm resolves at install time.
RICH_DEP_PREFIX = "("


def fetch(url: str, cache: pathlib.Path) -> bytes:
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    blob = cache / key
    if blob.exists():
        return blob.read_bytes()
    with urllib.request.urlopen(url, timeout=300) as response:
        data = response.read()
    cache.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(data)
    return data


def decompress(name: str, data: bytes) -> bytes:
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".zst"):
        import zstandard

        return zstandard.ZstdDecompressor().decompressobj().decompress(data)
    return data


def primary_of(baseurl: str, cache: pathlib.Path) -> tuple[bytes, dict]:
    """Return decompressed primary.xml plus the provenance of the index."""
    base = baseurl.rstrip("/") + "/"
    repomd = fetch(base + "repodata/repomd.xml", cache)
    root = ET.fromstring(repomd)
    revision = root.findtext(f"{REPO}revision")
    href = checksum = None
    for data in root.findall(f"{REPO}data"):
        if data.get("type") != "primary":
            continue
        location = data.find(f"{REPO}location").get("href")
        # zck is a delta format the stdlib cannot read; prefer anything else.
        if location.endswith(".zck"):
            continue
        href = location
        checksum = data.findtext(f"{REPO}checksum")
    if href is None:
        raise SystemExit(f"{base}: repomd.xml has no readable primary index")
    raw = fetch(base + href, cache)
    observed = hashlib.sha256(raw).hexdigest()
    provenance = {
        "baseurl": base,
        "revision": revision,
        "primary_href": href,
        "primary_sha256": observed,
        "primary_sha256_declared": checksum,
        "primary_bytes": len(raw),
    }
    return decompress(href, raw), provenance


def parse_primary(blob: bytes) -> dict:
    """Index a primary.xml into packages / provides / files."""
    packages: dict[str, dict] = {}
    provides: dict[str, set] = {}
    files: set[str] = set()
    for _, element in ET.iterparse(io.BytesIO(blob), events=("end",)):
        if element.tag != f"{COMMON}package":
            continue
        name = element.findtext(f"{COMMON}name")
        arch = element.findtext(f"{COMMON}arch")
        version = element.find(f"{COMMON}version")
        source = element.find(f"{COMMON}format/{RPM}sourcerpm")
        if arch != "i686":
            packages[name] = {
                "arch": arch,
                "evr": f"{version.get('ver')}-{version.get('rel')}",
                "srpm": source.text if source is not None else None,
                "requires": [
                    entry.get("name")
                    for entry in element.findall(
                        f"{COMMON}format/{RPM}requires/{RPM}entry"
                    )
                ],
            }
        for entry in element.findall(f"{COMMON}format/{RPM}provides/{RPM}entry"):
            provides.setdefault(entry.get("name"), set()).add(name)
        for shipped in element.findall(f"{COMMON}format/{COMMON}file"):
            files.add(shipped.text)
            provides.setdefault(shipped.text, set()).add(name)
        element.clear()
    return {"packages": packages, "provides": provides, "files": files}


def parse_source_primary(blob: bytes) -> dict:
    """srpm name -> BuildRequires:, from a Fedora *source* repository index."""
    result: dict[str, list[str]] = {}
    for _, element in ET.iterparse(io.BytesIO(blob), events=("end",)):
        if element.tag != f"{COMMON}package":
            continue
        name = element.findtext(f"{COMMON}name")
        result[name] = [
            entry.get("name")
            for entry in element.findall(
                f"{COMMON}format/{RPM}requires/{RPM}entry"
            )
        ]
        element.clear()
    return result


def srpm_name(srpm: str | None) -> str | None:
    """python-foo-1.2-3.fc45.src.rpm -> python-foo."""
    if not srpm:
        return None
    stem = srpm[: -len(".src.rpm")] if srpm.endswith(".src.rpm") else srpm
    return stem.rsplit("-", 2)[0]


def choose_provider(capability: str, candidates: set[str]) -> str:
    """Deterministic provider choice.

    An exact package-name match wins; otherwise the shortest name wins, ties
    broken alphabetically.  Recorded here rather than left implicit because the
    choice changes which source packages land in the build order.
    """
    if capability in candidates:
        return capability
    return sorted(candidates, key=lambda name: (len(name), name))[0]


def closure(roots, reference, have, source_index=None):
    """Transitive Requires: + BuildRequires: closure, stopping at what the target has.

    Runtime Requires: alone is not the closure a BUILD needs.  A build tool
    appears only in BuildRequires:, never in any runtime dependency, so a
    closure over Requires: cannot see it -- and the target is a base OS image,
    which by definition ships no build-only packages.

    What that cost, measured against Hummingbird's own index (22335
    capabilities) and Fedora's source index (23166 SRPMs): 413 capabilities the
    build order needs and nothing provides, blocking 462 of its 680 packages.
    The worst are not exotic --

        extra-cmake-modules  112 packages      vala        44
        kf6-rpm-macros       106               intltool    30
        gtk-doc               62               bison/flex  17 each

    -- and bison/flex are the tell: ordinary buildroot tools, absent from a
    base OS precisely because nothing at runtime needs them.  The first
    instance found in CI was Python's PEP-517 backends (#268, #269); it was one
    case of this class, not a special case.

    So the walk alternates until it reaches a fixpoint:

        runtime closure  ->  the source packages behind it  ->  their
        BuildRequires  ->  the binaries providing those  ->  runtime closure...

    Without `source_index` this is exactly the old runtime-only behaviour, so
    a caller with no source reference is unaffected.
    """
    packages = reference["packages"]
    provides = reference["provides"]
    seen: set[str] = set()
    folded_sources: set[str] = set()
    absent_roots: list[str] = []
    unresolved: dict[str, list[str]] = {}
    # A root may be a capability rather than a package name.  tunaOS's xfce
    # list says `thunar`; Fedora's package is `Thunar`, which Provides: thunar.
    resolved_roots = []
    for root in roots:
        if root in packages:
            resolved_roots.append(root)
        elif root in provides:
            resolved_roots.append(choose_provider(root, provides[root]))
        else:
            absent_roots.append(root)
    queue = list(resolved_roots)

    def walk_runtime() -> None:
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            info = packages.get(name)
            if info is None:
                absent_roots.append(name)
                continue
            for requirement in info["requires"]:
                if requirement in have:
                    continue
                if requirement.startswith(RICH_DEP_PREFIX):
                    continue
                candidates = provides.get(requirement)
                if not candidates:
                    unresolved.setdefault(requirement, []).append(name)
                    continue
                provider = choose_provider(requirement, candidates)
                if provider not in seen:
                    queue.append(provider)

    def fold_buildrequires() -> None:
        """Queue the providers of every BuildRequires: not yet reached.

        folded_sources keeps this from rescanning the whole of `seen` each
        time the runtime frontier drains -- with 23k SRPMs that turns a linear
        walk quadratic.
        """
        for binary in sorted(seen):
            info = packages.get(binary)
            if info is None:
                continue
            source = srpm_name(info.get("srpm"))
            if source is None or source in folded_sources:
                continue
            folded_sources.add(source)
            for requirement in source_index.get(source, ()):
                if requirement in have or requirement in seen:
                    continue
                if requirement.startswith(RICH_DEP_PREFIX):
                    continue
                candidates = provides.get(requirement)
                if not candidates:
                    unresolved.setdefault(requirement, []).append(source)
                    continue
                provider = choose_provider(requirement, candidates)
                if provider not in seen:
                    queue.append(provider)

    # Alternate to a fixpoint. The two halves are separate passes rather than
    # one loop with the fold hanging off the end of the body, because that is
    # what the previous shape was and it dropped the last fold every time:
    #
    #     while queue:
    #         name = queue.pop()
    #         if name in seen:
    #             continue          # <-- jumps past the fold below
    #         ...
    #         if not queue and source_index is not None:
    #             ...fold...
    #
    # When the queue drained on an already-seen name -- a duplicate, which is
    # common -- control went back to `while queue:`, found it empty and left,
    # with the newest sources never folded. Whether a desktop got its build
    # dependencies came down to whether the last pop happened to be a
    # duplicate, and that is why it looked like a per-desktop problem: cosmic
    # reached `vala` and ordered dconf after it at tier 11, gnome did not
    # reach it at all and sorted dconf into tier 0 with no build dependencies,
    # where it failed against Rawhide's Python 3.15 (see #287).
    while True:
        walk_runtime()
        if source_index is None:
            break
        fold_buildrequires()
        if not queue:
            break
        walk_runtime()
        if not queue:
            # The fold produced nothing new on the last pass; another fold
            # cannot either, since folded_sources only grows.
            if all(
                srpm_name(packages[b].get("srpm")) in folded_sources
                for b in seen
                if b in packages and packages[b].get("srpm")
            ):
                break
    return seen, absent_roots, unresolved


def tier_sources(need, reference, have, source_index=None):
    """Group source packages into build tiers.

    Build order is decided by BuildRequires:, not by runtime Requires:.  When
    --source-reference is available the SRPM entries in Fedora's source
    repository supply the real BuildRequires: (that is what rpm records for an
    arch "src" package), and the tiers below are a topological order over
    those.  Without it the function falls back to runtime Requires:, which is
    an approximation and is labelled as one in the report.

    Source-level cycles are real (glib2 BuildRequires gobject-introspection,
    which BuildRequires glib2).  They are collapsed into a single final tier
    and reported rather than silently dropped, because they are exactly the
    packages that need a bootstrap spec.
    """
    packages = reference["packages"]
    provides = reference["provides"]
    binary_to_source = {name: srpm_name(packages[name]["srpm"]) for name in need}
    edges: dict[str, set[str]] = collections.defaultdict(set)
    sources = {source for source in binary_to_source.values() if source}
    for source in sources:
        edges.setdefault(source, set())

    def link(source: str, requirement: str) -> None:
        if requirement in have or requirement.startswith(RICH_DEP_PREFIX):
            return
        candidates = provides.get(requirement)
        if not candidates:
            return
        provider = choose_provider(requirement, candidates)
        # BuildRequires: name -devel subpackages, which never enter the runtime
        # closure, so binary_to_source does not know them.  Resolve the
        # provider through the full reference index instead: cairo-devel maps
        # back to the cairo source package, which IS in the build set.
        dependency = binary_to_source.get(provider)
        if dependency is None and provider in packages:
            dependency = srpm_name(packages[provider]["srpm"])
        if dependency and dependency in sources and dependency != source:
            edges[source].add(dependency)

    if source_index:
        for source in sources:
            for requirement in source_index.get(source, ()):
                link(source, requirement)
    else:
        for binary, source in binary_to_source.items():
            if source:
                for requirement in packages[binary]["requires"]:
                    link(source, requirement)

    # Condense strongly connected components first.  Fedora's BuildRequires
    # graph has genuine cycles (cairo BuildRequires gtk-doc which eventually
    # BuildRequires cairo); a plain "who is ready" sweep dumps every package
    # downstream of any cycle into one undifferentiated tier.  Each SCC is a
    # real bootstrap unit and becomes exactly one tier, so a reader can see
    # which packages need --nocheck / bootstrap specs and which do not.
    components = strongly_connected(sources, edges)
    index_of = {name: i for i, comp in enumerate(components) for name in comp}
    condensed: dict[int, set[int]] = {i: set() for i in range(len(components))}
    for source, deps in edges.items():
        for dependency in deps:
            a, b = index_of[source], index_of[dependency]
            if a != b:
                condensed[a].add(b)

    tiers: list[list[str]] = []
    placed: set[int] = set()
    remaining = set(condensed)
    while remaining:
        ready = [i for i in remaining if condensed[i] <= placed]
        if not ready:  # cannot happen on a condensation; guard anyway
            tiers.append(sorted(n for i in remaining for n in components[i]))
            break
        tiers.append(sorted(n for i in ready for n in components[i]))
        placed |= set(ready)
        remaining -= set(ready)
    cycles = sorted(
        (sorted(comp) for comp in components if len(comp) > 1),
        key=lambda comp: (-len(comp), comp[0]),
    )
    return tiers, cycles


def strongly_connected(nodes, edges):
    """Tarjan's SCC, iterative so a 600-node graph cannot blow the stack."""
    order: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0
    for root in sorted(nodes):
        if root in order:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        order[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in order:
                    order[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(edges.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], order[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == order[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                result.append(component)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--catalog", type=pathlib.Path,
        default=pathlib.Path("manifests/hummingbird-desktops.yaml"),
    )
    parser.add_argument("--reference", default=DEFAULT_REFERENCE)
    parser.add_argument(
        "--source-reference", default=DEFAULT_SOURCE_REFERENCE,
        help="Fedora source repository, used for real BuildRequires: ordering. "
             "Pass '' to fall back to runtime-Requires ordering.",
    )
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--desktop", action="append", dest="desktops")
    parser.add_argument(
        "--cache", type=pathlib.Path,
        default=pathlib.Path(".cache/hummingbird-gap"),
    )
    parser.add_argument("--report-json", type=pathlib.Path)
    parser.add_argument("--build-order", type=pathlib.Path)
    args = parser.parse_args()

    catalog = yaml.safe_load(args.catalog.read_text())
    target = catalog["target"]
    baseurl = target["baseurl"].replace("$arch", args.arch).replace(
        "$basearch", args.arch
    )

    print(f"target       {target['id']}", file=sys.stderr)
    print(f"target repo  {baseurl}", file=sys.stderr)
    print(f"reference    {args.reference}", file=sys.stderr)

    target_blob, target_provenance = primary_of(baseurl, args.cache)
    target_index = parse_primary(target_blob)
    reference_blob, reference_provenance = primary_of(args.reference, args.cache)
    reference_index = parse_primary(reference_blob)
    source_index = None
    source_provenance = None
    if args.source_reference:
        source_blob, source_provenance = primary_of(args.source_reference, args.cache)
        source_index = parse_source_primary(source_blob)
        print(
            f"source reference has {len(source_index)} SRPMs with BuildRequires",
            file=sys.stderr,
        )

    have = set(target_index["provides"]) | set(target_index["packages"])
    print(
        f"target ships {len(target_index['packages'])} binary packages, "
        f"{len(target_index['provides'])} capabilities",
        file=sys.stderr,
    )
    print(
        f"reference has {len(reference_index['packages'])} binary packages",
        file=sys.stderr,
    )

    wanted = args.desktops or list(catalog["desktops"])
    report = {
        "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "target": {**target, "resolved_baseurl": baseurl},
        "target_index": target_provenance,
        "reference_index": reference_provenance,
        "source_reference_index": source_provenance,
        "tier_ordering": "buildrequires" if source_index else "runtime-requires",
        "target_binary_packages": len(target_index["packages"]),
        "desktops": {},
    }
    build_tiers: dict[str, list[list[str]]] = {}

    for desktop in wanted:
        definition = catalog["desktops"][desktop]
        roots = list(
            dict.fromkeys(
                definition.get("required_packages", [])
                + definition.get("install_packages", [])
            )
        )
        already = sorted(name for name in roots if name in target_index["packages"])
        need_roots = [name for name in roots if name not in target_index["packages"]]
        reachable, absent, unresolved = closure(
            need_roots, reference_index, have, source_index
        )
        # A root the reference does not carry (quickshell, dms — packaged
        # upstream, not in Fedora) is reported separately under
        # roots_absent_from_reference and cannot enter the build order, which
        # resolves everything through Fedora dist-git.
        need = sorted(
            name
            for name in reachable
            if name not in have and name in reference_index["packages"]
        )
        tiers, cycles = tier_sources(need, reference_index, have, source_index)
        sources = sorted({name for tier in tiers for name in tier})
        build_tiers[desktop] = tiers
        report["desktops"][desktop] = {
            "roots": roots,
            "roots_already_in_target": already,
            "roots_missing_from_target": need_roots,
            "roots_absent_from_reference": sorted(absent),
            "closure_binary_packages": len(reachable),
            "binary_packages_to_build": need,
            "source_packages_to_build": sources,
            "tiers": [{"index": i, "sources": tier} for i, tier in enumerate(tiers)],
            # Multi-member BuildRequires cycles.  These are the packages that
            # cannot be built in one pass from a clean buildroot: one member
            # needs a bootstrap spec (see src/gnome-50/glib2/glib2-bootstrap.spec
            # for the pattern already used here) or a --nocheck first pass.
            "buildrequires_cycles": cycles,
            "unresolved_capabilities": {
                capability: sorted(set(users))
                for capability, users in sorted(unresolved.items())
            },
        }
        print(
            f"{desktop:8} roots={len(roots)} already-in-target={len(already)} "
            f"binaries-to-build={len(need)} sources-to-build={len(sources)} "
            f"tiers={len(tiers)}",
            file=sys.stderr,
        )

    if args.report_json:
        args.report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"wrote {args.report_json}", file=sys.stderr)

    if args.build_order:
        emit_build_order(
            args.build_order, catalog, build_tiers, report,
            args.catalog.resolve().parents[1],
        )
        print(f"wrote {args.build_order}", file=sys.stderr)


def emit_build_order(path, catalog, build_tiers, report, root) -> None:
    """Write a build-chain.sh manifest whose tiers came out of the measurement.

    A package that already has a reviewed spec directory in this repository
    keeps it — src/gnome-50/gtk4 is a project-maintained Rawhide import with
    local fixes and must not be silently replaced by a fresh dist-git pull.
    Everything else is marked `distgit:`, which the build workflow imports with
    scripts/import-fedora-distgit.py before the tier runs.
    """
    target = catalog["target"]
    search = ["src/gnome-50", "src/deps", "src/hummingbird", "src/xfce-wayland"]
    cycles = {
        name: desktop
        for desktop, definition in report["desktops"].items()
        for cycle in definition["buildrequires_cycles"]
        for name in cycle
    }

    def locate(name):
        for prefix in search:
            if (root / prefix / name).is_dir():
                return f"{prefix}/{name}", False
        return f"src/hummingbird/{name}", True

    lines = [
        "# GENERATED BY scripts/measure-hummingbird-gap.py — DO NOT HAND-EDIT.",
        "#",
        "# Tiers are a topological order over the condensed BuildRequires: graph",
        "# of Fedora Rawhide's source repository, minus everything",
        f"# {target['id']} already ships.  Each tier builds in",
        "# parallel; tiers are sequential.  Regenerate with:",
        "#",
        "#   scripts/measure-hummingbird-gap.py \\",
        "#     --report-json docs/hummingbird-desktop-gap.json \\",
        "#     --build-order build-order-hummingbird-desktops.yml",
        "#",
        f"# Measured {report['measured_at']}",
        f"# target primary.xml   sha256 {report['target_index']['primary_sha256']}",
        f"# reference primary.xml sha256 {report['reference_index']['primary_sha256']}",
        "#",
        "# `distgit:` means the packaging is imported from Fedora Rawhide",
        "# dist-git at build time (scripts/import-fedora-distgit.py).  A package",
        "# with no `distgit:` key has a reviewed spec directory in this",
        "# repository and is built from that.  No COPR repository is enabled in",
        "# the buildroot or in the produced image.",
        "#",
        "# `bootstrap: true` marks a member of a BuildRequires cycle — see",
        "# docs/hummingbird-desktop-gap.json .buildrequires_cycles.  Those",
        "# packages cannot come up in one pass from a clean buildroot; the tier",
        "# they sit in needs a bootstrap spec or a second --force pass.",
        f"target: {target['id']}",
        f"r2_path: {target['r2_path']}",
        "",
        "tiers:",
    ]
    seen: set[str] = set()
    for desktop, tiers in build_tiers.items():
        for index, tier in enumerate(tiers):
            fresh = [name for name in tier if name not in seen]
            if not fresh:
                continue
            seen.update(fresh)
            lines.append(f"  - name: {desktop}-{index:02d}")
            lines.append("    packages:")
            for name in fresh:
                location, imported = locate(name)
                lines.append(f"      - path: {location}")
                if imported:
                    lines.append(f"        distgit: {name}")
                if name in cycles:
                    lines.append("        bootstrap: true")
            lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n")


if __name__ == "__main__":
    main()
