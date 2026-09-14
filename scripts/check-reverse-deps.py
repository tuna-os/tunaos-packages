#!/usr/bin/env python3
"""Would publishing this wave break anything already served?

Adapted from ebranch's `check-update` (slopfest/sandogasa, Apache-2.0 OR
MIT), which refuses to ship a side tag whose new builds leave reverse
dependencies uninstallable. Here the "side tag" is a staged wave and the
"tag" is the served index: before the wave lands, every package that
stays behind must still resolve against (served - replaced + wave).

Why this gate exists in THIS repository: the incidents it would have
caught are on record. The gnome50 bootstrap glib2 published an
`Obsoletes: glib2 < 2.87.3` that hijacked AppStream's glib2 in every
transaction (run 32405815822); the published libnotify 0.8.7 satisfied
gnome-settings-daemon's `>= 0.8.7` right up until the factory could no
longer rebuild it (#480). Both are the same shape: a publish changed the
capability universe and nothing asked what the change broke.

The check is index-arithmetic, deliberately: no dnf, no chroot, so it
can run inside a publisher in seconds. That buys three known blind
spots, recorded here rather than discovered later: rich `(a or b)`
dependencies are skipped (rpm resolves them at install time), a
capability provided WITHOUT a version cannot fail a versioned require,
and Obsoletes-driven replacement (the glib2 case's trigger) is reported
as informational `obsoletes_served` rather than resolved transactionally.
A fourth: provided EVRs are pooled per capability, not per provider, so
when a replaced package and a survivor provided the same capability the
replaced EVR still counts as offered. Every blind spot leans the same
way — the check can miss a breakage, it cannot invent one — which is
the right failure mode for a gate a publisher must pass.

Usage:
    # fully local, as publish-rpm-wave.sh runs it: the staged wave and the
    # synced-down served tree both carry repodata/
    scripts/check-reverse-deps.py --wave-repo out/wave --served-repo repo/
    # or resolve the served side from the factory contract / URLs
    scripts/check-reverse-deps.py --wave-repo out/wave \\
        --target el10 --arch x86_64
    scripts/check-reverse-deps.py --wave-repo out/wave \\
        --index https://repo.tunaos.org/repo/10/x86_64/ \\
        --system-index https://mirror.stream.centos.org/10-stream/AppStream/x86_64/os/

Exits non-zero when a served package that stays behind loses a
dependency, so a publisher can refuse the wave.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import lzma
import pathlib
import sys
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gap_engine  # noqa: E402
import published_index  # noqa: E402
import rpm_vercmp  # noqa: E402


def load(name: str, _filename: str):
    """Compatibility surface for callers of the former dynamic loader."""
    modules = {
        "gap": gap_engine,
        "gap_engine": gap_engine,
        "published_index": published_index,
        "rpm_vercmp": rpm_vercmp,
    }
    return modules[name]


def local_primary(repo: pathlib.Path) -> bytes:
    """The decompressed primary.xml of an on-disk repo directory."""
    gap = gap_engine
    repomd = (repo / "repodata" / "repomd.xml").read_bytes()
    root = ET.fromstring(repomd)
    for data in root.findall(f"{gap.REPO}data"):
        if data.get("type") != "primary":
            continue
        href = data.find(f"{gap.REPO}location").get("href")
        if href.endswith(".zck"):
            continue
        blob = (repo / href).read_bytes()
        if href.endswith(".gz"):
            return gzip.decompress(blob)
        if href.endswith(".xz"):
            return lzma.decompress(blob)
        return blob
    raise SystemExit(f"{repo}: repodata carries no readable primary index")


def parse_obsoletes(blob: bytes) -> dict[str, list[str]]:
    """package name -> the capability names it Obsoletes."""
    gap = gap_engine
    result: dict[str, list[str]] = {}
    for _, element in ET.iterparse(_bytes_io(blob), events=("end",)):
        if element.tag != f"{gap.COMMON}package":
            continue
        name = element.findtext(f"{gap.COMMON}name")
        entries = [
            entry.get("name")
            for entry in element.findall(
                f"{gap.COMMON}format/{gap.RPM}obsoletes/{gap.RPM}entry")
        ]
        if entries:
            result[name] = entries
        element.clear()
    return result


def _bytes_io(blob: bytes):
    import io
    return io.BytesIO(blob)


def _universe(indexes: list[tuple[dict, set]]) -> tuple[set, dict]:
    """Capabilities of a set of (parse_primary index, dropped names).

    A capability counts when at least one NON-dropped package provides
    it; a package name counts as a capability of itself.
    """
    caps: set[str] = set()
    caps_evr: dict[str, set] = collections.defaultdict(set)
    for index, dropped in indexes:
        caps.update(set(index["packages"]) - dropped)
        for cap, providers in index["provides"].items():
            if providers - dropped:
                caps.add(cap)
        for cap, evrs in index.get("provides_evr", {}).items():
            if index["provides"].get(cap, set()) - dropped:
                caps_evr[cap].update(evrs)
    return caps, caps_evr


def _unresolved(info, caps, caps_evr, vercmp) -> list[str]:
    missing = []
    for requirement in info.get("requires", ()):
        if requirement is None or requirement.startswith("("):
            continue
        if requirement.startswith("rpmlib("):
            continue
        if requirement not in caps:
            missing.append(requirement)
    for dep, op, required in info.get("requires_versioned", ()):
        offered = caps_evr.get(dep)
        if not offered:
            continue  # unversioned provider: not judgeable
        if not any(vercmp.satisfies(evr, op, required) for evr in offered):
            missing.append(f"{dep} {op} {required}")
    return missing


def simulate(served: dict, wave: dict, vercmp, system: dict | None = None) -> dict:
    """The dependency DELTA of publishing (served - replaced + wave).

    The check is differential on purpose: the factory's indexes are a
    thin layer over the target's system repositories, so most Requires
    resolve outside anything this gate can see. A dependency that was
    unresolvable within view before the wave and still is after tells
    us nothing; one that WAS resolvable and no longer is, is precisely
    what the wave broke. With `system` supplied (the target's system
    repo indexes), the view is complete and the wave's own packages are
    additionally required to resolve outright.
    """
    replaced = sorted(set(served["packages"]) & set(wave["packages"]))
    replaced_set = set(replaced)
    survivors = {
        name: info for name, info in served["packages"].items()
        if name not in replaced_set
    }

    base = [(system, set())] if system else []
    before_caps, before_evr = _universe(base + [(served, set())])
    after_caps, after_evr = _universe(
        base + [(served, replaced_set), (wave, set())])

    broken = {}
    for name in sorted(survivors):
        after = _unresolved(survivors[name], after_caps, after_evr, vercmp)
        if not after:
            continue
        before = set(_unresolved(survivors[name], before_caps, before_evr, vercmp))
        regressed = [dep for dep in after if dep not in before]
        if regressed:
            broken[name] = regressed

    wave_uninstallable = {}
    for name in sorted(wave["packages"]):
        after = _unresolved(wave["packages"][name], after_caps, after_evr, vercmp)
        if not after:
            continue
        if system is None:
            # Incomplete view: only deps the pre-wave factory universe
            # DID satisfy are judgeable — losing one of those is real.
            after = [dep for dep in after
                     if dep.split(" ")[0] in before_caps]
        if after:
            wave_uninstallable[name] = after

    return {
        "replaced": replaced,
        "broken_reverse_deps": broken,
        "wave_uninstallable": wave_uninstallable,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="refuse a wave that breaks the served index")
    parser.add_argument("--wave-repo", type=pathlib.Path, required=True,
                        help="staged wave directory containing repodata/")
    parser.add_argument("--target", help="target id from package-factory.yaml")
    parser.add_argument("--arch", help="architecture of the wave")
    parser.add_argument("--index", action="append", default=[],
                        help="served index URL (repeatable; default: the "
                             "target/arch's published_index)")
    parser.add_argument("--served-repo", action="append", default=[],
                        type=pathlib.Path,
                        help="a local synced-down copy of a served prefix "
                             "(repeatable; lets the gate run with no network)")
    parser.add_argument("--system-index", action="append", default=[],
                        help="the target's system repo URLs (repeatable). "
                             "With these the view is complete and the wave's "
                             "own packages must resolve outright")
    parser.add_argument("--cache", type=pathlib.Path,
                        default=pathlib.Path(".cache/reverse-deps"))
    parser.add_argument("--json", type=pathlib.Path)
    args = parser.parse_args()

    gap = gap_engine
    vercmp = rpm_vercmp

    urls = list(args.index)
    if not urls and not args.served_repo:
        if not (args.target and args.arch):
            raise SystemExit(
                "need --index, --served-repo, or --target with --arch")
        target = (published.load().get("targets") or {}).get(args.target) or {}
        urls = published.urls_for(target, args.arch)
        if not urls:
            # An unserved target has no reverse deps to break; say so
            # honestly instead of failing the first-ever publish.
            print(f"{args.target}/{args.arch} declares no published index; "
                  "nothing served, nothing to break")
            return 0

    def merged(blobs) -> dict:
        combined = {"packages": {}, "provides": {},
                    "provides_evr": {}, "files": set()}
        for blob in blobs:
            parsed = gap.parse_primary(blob)
            combined["packages"].update(parsed["packages"])
            for cap, providers in parsed["provides"].items():
                combined["provides"].setdefault(cap, set()).update(providers)
            for cap, evrs in parsed["provides_evr"].items():
                combined["provides_evr"].setdefault(cap, set()).update(evrs)
        return combined

    served = merged(
        [gap.primary_of(url, args.cache)[0] for url in urls]
        + [local_primary(repo) for repo in args.served_repo])
    system = None
    if args.system_index:
        system = merged(gap.primary_of(url, args.cache)[0]
                        for url in args.system_index)

    wave_blob = local_primary(args.wave_repo)
    wave = gap.parse_primary(wave_blob)

    report = simulate(served, wave, vercmp, system=system)

    # Informational: an Obsoletes against anything the combined universe
    # serves is the glib2-hijack shape. Not a failure by itself -- an
    # intentional rename uses exactly this -- but never silent again.
    served_names = set(served["packages"])
    obsoletes = {
        name: sorted(set(entries) & served_names)
        for name, entries in parse_obsoletes(wave_blob).items()
        if set(entries) & served_names
    }
    report["obsoletes_served"] = obsoletes

    print(f"wave: {len(wave['packages'])} package(s), "
          f"replaces {len(report['replaced'])} served name(s)")
    for name, missing in report["broken_reverse_deps"].items():
        print(f"  BREAKS {name}: loses {', '.join(missing)}")
    for name, missing in report["wave_uninstallable"].items():
        print(f"  wave package {name} cannot install: {', '.join(missing)}")
    for name, hit in obsoletes.items():
        print(f"  note: {name} Obsoletes served package(s): {', '.join(hit)}")

    if args.json:
        args.json.write_text(json.dumps(
            report, indent=2, sort_keys=True, default=sorted) + "\n")
        print(f"wrote {args.json}")

    if report["broken_reverse_deps"] or report["wave_uninstallable"]:
        print("\nthe wave does not keep the served index installable")
        return 1
    print("\nevery served package still resolves after this wave")
    return 0


if __name__ == "__main__":
    sys.exit(main())
