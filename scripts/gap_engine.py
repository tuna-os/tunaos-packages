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

Usage (always through the target-parameterized entry point):
    scripts/measure-target-gap.py --target <id> \
        --report-json docs/<id>-desktop-gap.json \
        --build-order build-order-<id>-desktops.yml
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

ROOT = pathlib.Path(__file__).resolve().parents[1]

COMMON = "{http://linux.duke.edu/metadata/common}"
RPM = "{http://linux.duke.edu/metadata/rpm}"
REPO = "{http://linux.duke.edu/metadata/repo}"

# Capabilities no rebuild can supply because they are the buildroot itself or
# an unversioned rich-dep alternative that rpm resolves at install time.
RICH_DEP_PREFIX = "("


def target_measurement(factory: dict, target_id: str) -> dict:
    """Return one target's declared gap-measurement contract.

    The desktop manifest remains the target-specific *roots* declaration. Its
    repository URLs used to make it a second target contract, though, which is
    exactly the per-family drift RFC 011 removes. A target enabled through the
    generic entry point declares those inputs next to its target contract.
    """
    target = factory.get("targets", {}).get(target_id)
    if target is None:
        raise SystemExit(f"unknown factory target: {target_id}")
    if target.get("repository") != "rpm-md":
        raise SystemExit(
            f"{target_id}: gap measurement currently supports rpm-md only"
        )
    measurement = target.get("gap_measurement")
    if not isinstance(measurement, dict):
        raise SystemExit(
            f"{target_id}: no gap_measurement contract; add roots_manifest, "
            "target_index and reference_index before enabling this target"
        )
    required = ("roots_manifest", "target_index", "reference_index")
    missing = [key for key in required if not measurement.get(key)]
    if missing:
        raise SystemExit(
            f"{target_id}: incomplete gap_measurement contract: missing "
            f"{', '.join(missing)}"
        )
    return measurement


def fetch(url: str, cache: pathlib.Path) -> bytes:
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    blob = cache / key
    if blob.exists():
        return blob.read_bytes()
    # A named User-Agent, because the served indexes sit behind Cloudflare
    # and some egress paths get the default Python-urllib agent answered
    # with 403 while curl from the same host gets 200. Measured, not
    # assumed. Same agent string bump-github-release-recipe.py sends.
    request = urllib.request.Request(
        url, headers={"User-Agent": "tunaos-package-factory"})
    with urllib.request.urlopen(request, timeout=300) as response:
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


def _oci():
    """scripts/oci_repository.py, loaded by path: scripts/ is not a package,
    and every consumer of this engine loads IT by path too."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "oci_repository", pathlib.Path(__file__).resolve().parent / "oci_repository.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def primary_of(baseurl: str, cache: pathlib.Path, arch: str = "x86_64") -> tuple[bytes, dict]:
    """Return decompressed primary.xml plus the provenance of the index.

    `oci://host/path@sha256:...` names a repository that ships INSIDE an OCI
    image (utah-packages' layout); it is read straight out of the registry by
    scripts/oci_repository.py and comes back in this same shape.
    """
    if baseurl.startswith("oci://"):
        return _oci().primary_of(baseurl, cache, arch)
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


# primary.xml dependency-entry flags -> RPM comparison operators.
FLAG_OPS = {"EQ": "=", "GE": ">=", "LE": "<=", "GT": ">", "LT": "<"}


def entry_evr(entry) -> str | None:
    """The epoch:version[-release] a versioned dependency entry names."""
    ver = entry.get("ver")
    if not ver:
        return None
    evr = f"{entry.get('epoch') or '0'}:{ver}"
    rel = entry.get("rel")
    return f"{evr}-{rel}" if rel else evr


def parse_primary(blob: bytes) -> dict:
    """Index a primary.xml into packages / provides / files.

    `provides_evr` records, per capability, the set of EVRs it is
    provided at — only for entries that carry a version. A capability
    provided without a version cannot be judged against a constraint
    and is deliberately absent, so version checks treat it as
    satisfiable rather than inventing a verdict.
    """
    packages: dict[str, dict] = {}
    provides: dict[str, set] = {}
    provides_evr: dict[str, set] = {}
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
                "requires_versioned": [
                    (entry.get("name"), FLAG_OPS[entry.get("flags")],
                     entry_evr(entry))
                    for entry in element.findall(
                        f"{COMMON}format/{RPM}requires/{RPM}entry"
                    )
                    if entry.get("flags") in FLAG_OPS
                    and entry_evr(entry) is not None
                ],
            }
        for entry in element.findall(f"{COMMON}format/{RPM}provides/{RPM}entry"):
            provides.setdefault(entry.get("name"), set()).add(name)
            evr = entry_evr(entry)
            if evr is not None:
                provides_evr.setdefault(entry.get("name"), set()).add(evr)
        for shipped in element.findall(f"{COMMON}format/{COMMON}file"):
            files.add(shipped.text)
            provides.setdefault(shipped.text, set()).add(name)
        element.clear()
    return {"packages": packages, "provides": provides,
            "provides_evr": provides_evr, "files": files}


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


def parse_source_primary_versioned(blob: bytes) -> dict:
    """srpm name -> [(capability, op, required EVR)], versioned BRs only.

    The name-level index above answers "does anything provide this at
    all"; this one carries the constraints, so a preflight can also
    answer "at a version that satisfies" — the libnotify >= 0.8.7 class
    (#480), where the capability exists everywhere and satisfies
    nothing.
    """
    result: dict[str, list[tuple[str, str, str]]] = {}
    for _, element in ET.iterparse(io.BytesIO(blob), events=("end",)):
        if element.tag != f"{COMMON}package":
            continue
        name = element.findtext(f"{COMMON}name")
        versioned = []
        for entry in element.findall(f"{COMMON}format/{RPM}requires/{RPM}entry"):
            op = FLAG_OPS.get(entry.get("flags") or "")
            evr = entry_evr(entry)
            if op and evr:
                versioned.append((entry.get("name"), op, evr))
        result[name] = versioned
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


def consumed_indexes(measurement: dict | None, arch: str, cache: pathlib.Path) -> list[dict]:
    """Parse every `consumed_indexes` entry of a gap_measurement contract.

    Each entry is {id, index[, note]} where `index` is an https:// rpm-md
    baseurl or an oci://…@sha256:… reference; `$arch` is substituted. Returns
    one dict per entry with the index's provenance, its binary count, and a
    private `_have` set (provides ∪ names) for the caller to fold in.
    """
    out = []
    for spec in (measurement or {}).get("consumed_indexes") or []:
        url = spec["index"].replace("$arch", arch).replace("$basearch", arch)
        blob, provenance = primary_of(url, cache, arch)
        index = parse_primary(blob)
        out.append({
            "id": spec["id"],
            **provenance,
            "binary_packages": len(index["packages"]),
            "_have": set(index["provides"]) | set(index["packages"]),
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--catalog", type=pathlib.Path,
        help="Target roots manifest; --target resolves this from "
             "package-factory.yaml instead.",
    )
    parser.add_argument(
        "--factory", type=pathlib.Path,
        default=ROOT / "manifests" / "package-factory.yaml",
        help="Factory target contract used with --target.",
    )
    parser.add_argument(
        "--target",
        help="Factory target to measure. Its gap_measurement contract supplies "
             "the roots and repository indexes.",
    )
    parser.add_argument("--reference")
    parser.add_argument(
        "--source-reference",
        help="Fedora source repository, used for real BuildRequires: ordering. "
             "Pass '' to fall back to runtime-Requires ordering.",
    )
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument(
        "--membership", choices=("selfhost", "runtime"), default=None,
        help="What the build order contains.  'selfhost': the full "
             "Requires: + BuildRequires: closure, so every build tool is "
             "built here too.  'runtime': only what images ship; build "
             "tools come from the buildroot's own repositories (the "
             "target's mock config declares them).  Defaults to the "
             "catalog's `membership:` key, then 'selfhost'.",
    )
    parser.add_argument("--desktop", action="append", dest="desktops")
    parser.add_argument(
        "--cache", type=pathlib.Path,
        default=pathlib.Path(".cache/target-gap"),
    )
    parser.add_argument("--report-json", type=pathlib.Path)
    parser.add_argument("--build-order", type=pathlib.Path)
    args = parser.parse_args()

    # No hard-coded target. The engine measures whichever target the
    # contract (or explicit flags) names — a default here is how one
    # target's assumptions become the pipeline (the hummingbird fallbacks
    # this replaces lasted long enough to name the engine after them).
    measurement = None
    if args.target:
        factory = yaml.safe_load(args.factory.read_text(encoding="utf-8"))
        measurement = target_measurement(factory, args.target)
    if not measurement and not args.catalog:
        raise SystemExit(
            "no target given: pass --target <id> (whose gap_measurement "
            "contract in manifests/package-factory.yaml supplies roots and "
            "indexes, via scripts/measure-target-gap.py) or an explicit "
            "--catalog with --reference/--source-reference")
    catalog_path = args.catalog or pathlib.Path(measurement["roots_manifest"])
    reference = args.reference or (
        measurement.get("reference_index") if measurement else None)
    if not reference:
        raise SystemExit("no reference index: the target's gap_measurement "
                         "contract must declare reference_index, or pass "
                         "--reference")
    source_reference = args.source_reference
    if source_reference is None:
        source_reference = (
            measurement.get("source_reference_index", "") if measurement
            else "")

    catalog = yaml.safe_load(catalog_path.read_text())
    membership = args.membership or catalog.get("membership") or "selfhost"
    target = dict(catalog["target"])
    if measurement:
        target["baseurl"] = measurement["target_index"]
        if target["id"] != args.target and not target["id"].startswith(args.target + "-"):
            raise SystemExit(
                f"{args.target}: roots manifest identifies {target['id']}; "
                "use a manifest for this target"
            )
    baseurl = target["baseurl"].replace("$arch", args.arch).replace(
        "$basearch", args.arch
    )

    print(f"target       {target['id']}", file=sys.stderr)
    print(f"target repo  {baseurl}", file=sys.stderr)
    print(f"reference    {reference}", file=sys.stderr)
    print(f"membership   {membership}", file=sys.stderr)

    target_blob, target_provenance = primary_of(baseurl, args.cache)
    target_index = parse_primary(target_blob)
    reference_blob, reference_provenance = primary_of(reference, args.cache)
    reference_index = parse_primary(reference_blob)
    source_index = None
    source_provenance = None
    if source_reference:
        source_blob, source_provenance = primary_of(source_reference, args.cache)
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
    # Repositories a consumer image enables NEXT TO the target, declared in
    # the target contract as `consumed_indexes` (utah-packages for
    # hummingbird's GNOME). Whatever they ship is not a gap: the closure
    # treats it as already had, exactly like the target's own index. It is
    # NOT added to the reference, so nothing in the build order is ever
    # resolved through a consumed repository's BuildRequires.
    consumed = consumed_indexes(measurement, args.arch, args.cache)
    for entry in consumed:
        have |= entry.pop("_have")
        print(
            f"consumed     {entry['id']}: {entry['binary_packages']} binary "
            f"packages counted as had ({entry['baseurl']})",
            file=sys.stderr,
        )
    print(
        f"reference has {len(reference_index['packages'])} binary packages",
        file=sys.stderr,
    )

    wanted = args.desktops or list(catalog["desktops"])
    report = {
        "measured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "target": {**target, "resolved_baseurl": baseurl},
        "target_index": target_provenance,
        "reference_index": reference_provenance,
        "source_reference_index": source_provenance,
        "tier_ordering": "buildrequires" if source_index else "runtime-requires",
        "membership": membership,
        "target_binary_packages": len(target_index["packages"]),
        "consumed_indexes": consumed,
        "desktops": {},
    }
    build_tiers: dict[str, list[list[str]]] = {}
    union_need: set[str] = set()

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
        # Membership and ordering are separate questions.  'runtime'
        # membership walks Requires: only -- the build order then contains
        # exactly what images ship, and BuildRequires: (bison, transfig,
        # gtk-doc, the Java documentation stack) are satisfied by the
        # buildroot's inherited Rawhide fallback instead of being rebuilt
        # here.  Ordering below always uses the real BuildRequires: graph
        # regardless, so members that build-depend on each other still come
        # up in the right tiers.
        reachable, absent, unresolved = closure(
            need_roots, reference_index, have,
            source_index if membership == "selfhost" else None,
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
        union_need |= set(need)
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

    # Tier once, over every desktop's packages at the same time.
    #
    # Tiering per desktop and concatenating the results is not merely longer,
    # it is wrong. tier_sources only draws an edge when the dependency is in
    # the set being tiered, so each desktop saw a different subgraph: an edge
    # to a package that another desktop happened to claim simply was not there.
    # The old emitter then deduplicated across desktops, which put a shared
    # build tool in whichever desktop's tiering reached it first and left every
    # earlier package that needed it building without it.
    #
    # Measured on the 78-tier manifest this produced: 993 BuildRequires edges,
    # touching 405 of 1248 source packages, whose dependency was built in a
    # LATER tier. nautilus in gnome-09 BuildRequires desktop-file-utils, which
    # the manifest built in kde-00, nine tiers later. Those builds did not
    # fail -- the buildroot also has fedora-rawhide, so they quietly linked
    # against Fedora's copy instead of the one this manifest exists to build.
    #
    # Over the union every edge is present, so the layering is a topological
    # order over the whole graph. It is also 36 layers rather than 78.
    global_tiers, global_cycles = tier_sources(
        sorted(union_need), reference_index, have, source_index
    )
    report["build_order"] = {
        "tiers": [{"index": i, "sources": t} for i, t in enumerate(global_tiers)],
        "buildrequires_cycles": global_cycles,
    }
    print(
        f"build order: {len(global_tiers)} layers over {len(union_need)} binary "
        f"packages from {len(wanted)} desktops",
        file=sys.stderr,
    )

    if args.report_json:
        args.report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"wrote {args.report_json}", file=sys.stderr)

    if args.build_order:
        emit_build_order(
            args.build_order, catalog, global_tiers, global_cycles, report,
            # catalog_path, NOT args.catalog: the generic entrypoint
            # (measure-target-gap.py --target hummingbird) passes no --catalog
            # and args.catalog is then None -- which crashed the scheduled
            # drift re-measure right after it wrote the report.
            catalog_path.resolve().parents[1],
            target_name=args.target,
        )
        print(f"wrote {args.build_order}", file=sys.stderr)


def emit_build_order(path, catalog, global_tiers, global_cycles, report, root,
                     target_name=None) -> None:
    """Write a build-chain.sh manifest whose tiers came out of the measurement.

    A package that already has a reviewed spec directory in this repository
    keeps it — src/gnome-51/gtk4 is a project-maintained Rawhide import with
    local fixes and must not be silently replaced by a fresh dist-git pull.
    Everything else is marked `distgit:`, which the build workflow imports with
    scripts/import-fedora-distgit.py before the tier runs.  A name listed in
    the roots manifest's `prefer_distgit:` is imported even if such a
    directory exists: it belongs to another target.
    """
    target = catalog["target"]
    # Where reviewed spec directories live and where dist-git imports land
    # are properties of the TARGET's source tree, so the roots manifest
    # declares them — hard-coding one family's layout here is how the
    # engine ended up named after hummingbird.
    search = catalog.get("source_paths")
    fallback = catalog.get("distgit_prefix")
    if not search or not fallback:
        raise SystemExit(
            f"{target['id']}: --build-order needs `source_paths:` and "
            "`distgit_prefix:` declared in the roots manifest")
    # A spec directory under a shared prefix may exist for a DIFFERENT
    # target and carry a pin this one must not inherit.  src/deps/libnotify
    # is pinned to 0.8.7 for EL10 -- its own comment says "0.8.7 rather than
    # the newer 0.8.8 deliberately" -- while a Rawhide rebuild wants 0.8.8.
    # `prefer_distgit:` is how a roots manifest says "always import this one",
    # so the shared directory cannot silently downgrade it.
    always_distgit = set(catalog.get("prefer_distgit") or [])
    cycles = {name for cycle in global_cycles for name in cycle}

    def locate(name):
        if name not in always_distgit:
            for prefix in search:
                if (root / prefix / name).is_dir():
                    return f"{prefix}/{name}", False
        return f"{fallback}/{name}", True

    target_id = target_name or target["id"].split("-")[0]
    lines = [
        f"# GENERATED BY scripts/measure-target-gap.py --target {target_id} — DO NOT HAND-EDIT.",
        "#",
        "# Tiers are ONE topological order over the condensed BuildRequires:",
        "# graph of every desktop at once -- not one order per desktop glued",
        "# end to end, which is what this file used to be and which built 405",
        "# packages before something they BuildRequire.  Minus everything",
        f"# {target['id']} already ships.  Each tier builds in",
        "# parallel; tiers are sequential.  Regenerate with:",
        "#",
        f"#   scripts/measure-target-gap.py --target {target_id} \\",
        f"#     --report-json docs/{target_id}-desktop-gap.json \\",
        f"#     --build-order {path.name}",
        "#",
        f"# Measured {report['measured_at']}",
        f"# target primary.xml   sha256 {report['target_index']['primary_sha256']}",
        f"# reference primary.xml sha256 {report['reference_index']['primary_sha256']}",
        *[f"# consumed {c['id']:<14} {c['baseurl']} (primary sha256 {c['primary_sha256'][:12]}, "
          f"{c['binary_packages']} binaries counted as had)"
          for c in report.get("consumed_indexes", [])],
        "#",
        "# `distgit:` means the packaging is imported from Fedora Rawhide",
        "# dist-git at build time (scripts/import-fedora-distgit.py).  A package",
        "# with no `distgit:` key has a reviewed spec directory in this",
        "# repository and is built from that.  No COPR repository is enabled in",
        "# the buildroot or in the produced image.",
        "#",
        "# `bootstrap: true` marks a member of a BuildRequires cycle — see",
        f"# docs/{target_id}-desktop-gap.json .buildrequires_cycles.  Those",
        "# packages cannot come up in one pass from a clean buildroot; the tier",
        "# they sit in needs a bootstrap spec or a second --force pass.",
        f"target: {target['id']}",
        f"r2_path: {target['r2_path']}",
        "",
        "tiers:",
    ]
    if report.get("membership") == "runtime":
        marker = lines.index(f"# Measured {report['measured_at']}")
        lines[marker:marker] = [
            "# Membership is `runtime`: only what images ship is built here.",
            "# BuildRequires-only tools (bison, transfig, gtk-doc, ...) come",
            "# from the buildroot's inherited Rawhide fallback at priority 99",
            "# (the target's mock config), never from this build order.",
            "#",
        ]
    # The declared bootstrap tiers come first and verbatim.  They are the
    # PEP-517 backends the measurement cannot discover (catalog `bootstrap:`),
    # and they used to be hand-written into this generated file, where the next
    # regeneration deleted them.
    seen: set[str] = set()
    for tier in catalog.get("bootstrap", []):
        lines.append(f"  - name: {tier['name']}")
        lines.append("    packages:")
        for pkg in tier["packages"]:
            lines.append(f"      - path: {pkg['path']}")
            if "distgit" in pkg:
                lines.append(f"        distgit: {pkg['distgit']}")
            seen.add(pkg.get("distgit") or pkg["path"].rsplit("/", 1)[-1])
        lines.append("")

    # Then the layers, which are a single topological order over every
    # desktop's packages rather than five per-desktop orders concatenated.
    # `seen` only skips what the bootstrap tiers already built; there is no
    # cross-desktop deduplication left to do, because there are no longer
    # five separate orders to deduplicate between.
    for index, tier in enumerate(global_tiers):
        fresh = [name for name in tier if name not in seen]
        if not fresh:
            continue
        seen.update(fresh)
        lines.append(f"  - name: layer-{index:02d}")
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
