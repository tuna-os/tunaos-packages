#!/usr/bin/env python3
"""Does what we publish for Hummingbird actually resolve on Hummingbird?

The build chain answers "did it compile"; the gap engine answers "what is
missing from the target"; nothing answered the question a consumer asks: with
ONLY the target's own repository and our published prefix enabled, do every
desktop root and every explicitly retained served package resolve?
utah-packages gates its publish on the desktop question inside the bootc-os
image. This is the static half of that gate plus the served-package repair
set: it walks each declared runtime closure over the union of the target and
published indexes. No container and no network beyond the declared indexes,
so it runs in CI on every change and on a schedule.

It is necessary, not sufficient: version constraints and conflicts are
dnf's job.  An empty unresolved list means "dnf has a chance"; a non-empty
one names exactly what to build next -- or what to REBUILD: classify()
separates a capability nothing provides at any version (build it) from one
whose library IS provided and whose symbol version or soname is not (our
own output, built against an ABI the target does not ship: #629's
`libm.so.6(GLIBC_2.44)`, still served by the prefix on 2026-10-24).

Measured 2026-09-02 against the x86_64 indexes (docs/HUMMINGBIRD-TARGET.md):
the published prefix left 30 capabilities unresolved for the 58 GNOME roots,
three of them libm.so.6(GLIBC_2.44) -- a Rawhide-buildroot leak no amount of
further building would have fixed.

Usage:
  scripts/check-hummingbird-installability.py [--arch x86_64] [--desktop gnome ...]
      [--json] [--fail-on-unresolved]
  scripts/check-hummingbird-installability.py --published-index URL --target-index URL
      (defaults come from manifests/package-factory.yaml)
  scripts/check-hummingbird-installability.py --published-index file://… \
      --fail-on-regression
      (the publish gate: the wave's own index, against
       docs/hummingbird-installability-baseline.json -- no desktop may lose a
       capability it could resolve before the wave)
  scripts/check-hummingbird-installability.py --arch ARCH --write-baseline
      (refresh the recorded state for one architecture)
Exit status 0 unless --fail-on-unresolved and something is unresolved, or
--fail-on-regression and a desktop went backwards; the report is the product.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("gap_engine", ROOT / "scripts" / "gap_engine.py")
gap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gap)


RETAINED_SET = "retained"

def merge_indexes(*indexes: dict) -> dict:
    """Union of parsed primary.xml indexes; the first wins on a name collision."""
    out = {"packages": {}, "provides": {}, "provides_evr": {}, "files": set()}
    for index in reversed(indexes):
        out["packages"].update(index["packages"])
        for cap, providers in index["provides"].items():
            out["provides"].setdefault(cap, set()).update(providers)
        for cap, evrs in index.get("provides_evr", {}).items():
            out["provides_evr"].setdefault(cap, set()).update(evrs)
        out["files"] |= index.get("files", set())
    return out


def roots_of(catalog: dict, package_set: str) -> list[str]:
    if package_set == RETAINED_SET:
        return list(dict.fromkeys(catalog.get("retained_packages") or []))
    definition = catalog["desktops"][package_set]
    return list(dict.fromkeys(
        definition.get("required_packages", []) + definition.get("install_packages", [])
    ))


# `libm.so.6(GLIBC_2.44)(64bit)` -- a soname plus the symbol version the
# needer was linked against. `libvorbis.so.0()(64bit)` -- a bare soname.
# Package-name capabilities (`poppler-data`, `gstreamer1-plugins-bad-free-libs
# (x86-64)`) match neither and are never an ABI question.
SONAME_SYMVER = re.compile(r"^(?P<name>[^()]+\.so[0-9.]*)\((?P<symver>[^()]+)\)"
                           r"(?P<arch>\(\d+bit\))?$")
SONAME = re.compile(r"^(?P<stem>[^()]+\.so)\.(?P<version>[0-9][0-9.]*)\(\)"
                    r"(?P<arch>\(\d+bit\))?$")


def soname_families(provides) -> dict:
    """(stem, arch) -> the set of sonames the universe provides under it.

    Built once per walk so classify() is a dict lookup rather than a scan of
    every provided capability.
    """
    families: dict[tuple[str, str], set[str]] = {}
    for cap in provides:
        match = SONAME.match(cap)
        if match:
            families.setdefault(
                (match["stem"], match["arch"] or ""), set()).add(cap)
    return families


def classify(cap: str, provides, families: dict) -> str:
    """Why nothing provides `cap` -- and therefore what fixes it.

    `abi_symbol`  the library IS provided, the symbol version is not: the
                  needer was built against a newer ABI than the target ships.
                  This is the GLIBC_2.44 shape (#629): a REBUILD in the
                  target's own root fixes it, building more packages does not.
    `abi_soname`  the same library name is provided at a different soname:
                  the needer was built against another major version. Also a
                  rebuild, against what the consumer universe actually serves.
    `absent`      nothing provides the name at any version: a package that has
                  to be BUILT (libcanberra, samba, evolution-data-server).
    """
    match = SONAME_SYMVER.match(cap)
    if match:
        arch = match["arch"] or ""
        if f"{match['name']}(){arch}" in provides:
            return "abi_symbol"
        soname, stem = match["name"], None
    else:
        match = SONAME.match(cap)
        if not match:
            return "absent"
        arch = match["arch"] or ""
        soname, stem = f"{match['stem']}.{match['version']}", match["stem"]
    if stem is None:
        stem = soname.rsplit(".so", 1)[0] + ".so"
    served = families.get((stem, arch), ())
    if any(provided != f"{soname}(){arch}" for provided in served):
        return "abi_soname"
    return "absent"


# A capability our own published prefix needs and the consumer universe
# cannot satisfy at that ABI: our output, built wrong, not a missing build.
REBUILD_CLASSES = ("abi_symbol", "abi_soname")


def rebuild_list(report: dict) -> dict:
    """Published packages to rebuild, with the capabilities that name them.

    docs/HUMMINGBIRD-TARGET.md §6 asked for exactly this list ("the checker's
    `needer_from: published` column"); it was a column to read by eye.
    """
    packages: dict[str, set[str]] = {}
    for desktop in report.values():
        for cap, detail in desktop["unresolved"].items():
            if detail["class"] not in REBUILD_CLASSES:
                continue
            if "published" not in detail["needer_from"]:
                continue
            for needer in detail["needed_by"]:
                packages.setdefault(needer, set()).add(cap)
    return {name: sorted(caps) for name, caps in sorted(packages.items())}


def check(catalog: dict, target_index: dict, published_index: dict,
          package_sets: list[str], consumed: list[dict] | None = None) -> dict:
    """Per package set: absent roots, closure size, and unresolved capabilities.

    `consumed` are the parsed indexes of repositories a consumer image enables
    next to the target and our prefix (utah-packages for GNOME). On a name
    collision they WIN: tunaOS enables utah at priority 4, our prefix at 5
    and the base's own repository at 10 (manifests/desktops/gnome.yaml
    there), so a name utah ships masks the prefix's copy exactly as dnf's
    by-name priority filter would. The target still provides, and a needer
    that lives in a consumed repo is attributed to `consumed`.
    """
    consumed = consumed or []
    universe = merge_indexes(*consumed, published_index, target_index)
    families = soname_families(universe["provides"])
    report = {}
    for package_set in package_sets:
        roots = roots_of(catalog, package_set)
        reachable, absent, unresolved = gap.closure(roots, universe, set())
        # Which repo would serve each unresolved capability's needer is the
        # actionable bit: a needer in OUR prefix means our package was linked
        # against something the target lacks (the GLIBC_2.44 shape); a needer
        # in the target's own repo is the base OS depending on something
        # neither ships.
        detail = {}
        for cap, needers in sorted(unresolved.items()):
            detail[cap] = {
                "needed_by": sorted(needers)[:5],
                # Same precedence as the universe: a name a consumed repo
                # ships is that repo's package, whatever the prefix also
                # carries under the name.
                "needer_from": sorted({
                    "consumed" if any(n in c["packages"] for c in consumed)
                    else "published" if n in published_index["packages"]
                    else "target"
                    for n in needers
                }),
                # Build it, or rebuild what needs it: classify() separates
                # the two, because only one of them is a bigger build order.
                "class": classify(cap, universe["provides"], families),
            }
        report[package_set] = {
            "roots": len(roots),
            "roots_absent": sorted(absent),
            "closure_packages": len(reachable),
            "unresolved": detail,
            "leaks": sorted(
                cap for cap, d in detail.items()
                if d["class"] in REBUILD_CLASSES and "published" in d["needer_from"]
            ),
            "resolvable": not absent and not unresolved,
        }
    return report


def render(report: dict) -> str:
    lines = ["| Package set | Roots | Absent roots | Closure | Unresolved capabilities "
             "| To rebuild | Resolvable |",
             "|---|---|---|---|---|---|---|"]
    for package_set, result in report.items():
        lines.append(f"| `{package_set}` | {result['roots']} | {len(result['roots_absent'])} | "
                     f"{result['closure_packages']} | {len(result['unresolved'])} | "
                     f"{len(result['leaks'])} | "
                     f"{'✅' if result['resolvable'] else '❌'} |")
    for package_set, result in report.items():
        if result["roots_absent"] or result["unresolved"]:
            lines.append("")
            lines.append(f"**{package_set}** — first blockers:")
            for name in result["roots_absent"][:10]:
                lines.append(f"- root `{name}`: in neither repository")
            for cap, detail in list(result["unresolved"].items())[:15]:
                lines.append(
                    f"- `{cap}` needed by "
                    f"{', '.join(f'`{n}`' for n in detail['needed_by'][:3])} "
                    f"({'/'.join(detail['needer_from'])}, {detail['class']})")
    rebuild = rebuild_list(report)
    if rebuild:
        lines.append("")
        lines.append("**Rebuild in the target's own root** — these are published "
                     "packages whose ABI the consumer universe does not serve, so "
                     "building more packages cannot fix them:")
        for name, caps in rebuild.items():
            lines.append(f"- `{name}`: {', '.join(f'`{c}`' for c in caps)}")
    return "\n".join(lines)


def baseline_of(report: dict) -> dict:
    """The comparable shape: what each desktop could not resolve.

    Counts are not the contract -- the set is. A wave that resolves one
    capability and breaks another keeps the count and is a regression.
    """
    return {
        desktop: {
            "roots_absent": list(r["roots_absent"]),
            "unresolved": sorted(r["unresolved"]),
            "leaks": list(r["leaks"]),
            "resolvable": r["resolvable"],
        }
        for desktop, r in report.items()
    }


def compare(baseline: dict, report: dict) -> dict:
    """Per desktop: what this measurement broke, and what it fixed.

    A desktop the baseline does not mention is new and cannot regress; a
    desktop the baseline records as resolvable regresses the moment it is
    not, even if the capability it lost is new.
    """
    current = baseline_of(report)
    verdict = {}
    for desktop, now in current.items():
        was = baseline.get(desktop)
        if was is None:
            verdict[desktop] = {"new": True, "regressions": [], "fixed": [],
                                "lost_resolvable": False}
            continue
        regressions = sorted(
            (set(now["unresolved"]) - set(was["unresolved"]))
            | (set(now["roots_absent"]) - set(was["roots_absent"]))
        )
        fixed = sorted(
            (set(was["unresolved"]) - set(now["unresolved"]))
            | (set(was["roots_absent"]) - set(now["roots_absent"]))
        )
        verdict[desktop] = {
            "new": False,
            "regressions": regressions,
            "fixed": fixed,
            "lost_resolvable": bool(was["resolvable"]) and not now["resolvable"],
        }
    return verdict


def regressed(verdict: dict) -> bool:
    return any(v["regressions"] or v["lost_resolvable"] for v in verdict.values())


def render_comparison(verdict: dict) -> str:
    lines = []
    for desktop, v in sorted(verdict.items()):
        if v["new"]:
            lines.append(f"- `{desktop}`: not in the baseline; recorded, not gated.")
            continue
        if v["lost_resolvable"]:
            lines.append(f"- `{desktop}`: **was resolvable and is not any more**.")
        for cap in v["regressions"]:
            lines.append(f"- `{desktop}`: **new** blocker `{cap}`")
        for cap in v["fixed"]:
            lines.append(f"- `{desktop}`: fixed `{cap}`")
    return "\n".join(lines) or "- no change against the baseline."


BASELINE_ABOUT = (
    "What each Hummingbird desktop could NOT resolve when this was measured, "
    "per architecture. The publish gate compares a wave against it: a wave may "
    "fix blockers, it may not add one. Refresh with "
    "scripts/check-hummingbird-installability.py --arch ARCH --write-baseline."
)


def read_baseline(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"about": BASELINE_ABOUT, "arches": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def write_baseline(path: pathlib.Path, arch: str, measurement: dict,
                   report: dict) -> None:
    """Record one architecture, leave the others alone.

    Each arch carries the index provenance it was measured against, because a
    baseline whose origin is unknown is a number nobody can re-derive.
    """
    recorded = read_baseline(path)
    recorded["about"] = BASELINE_ABOUT
    recorded.setdefault("arches", {})[arch] = {
        "measured_at": measurement["measured_at"],
        "target_index": measurement["target_index"],
        "published_index": measurement["published_index"],
        "consumed_indexes": measurement["consumed_indexes"],
        "desktops": baseline_of(report),
    }
    path.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--factory", type=pathlib.Path, default=ROOT / "manifests" / "package-factory.yaml")
    ap.add_argument("--catalog", type=pathlib.Path, default=ROOT / "manifests" / "hummingbird-desktops.yaml")
    ap.add_argument("--target", default="hummingbird")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--target-index", help="override the target's gap_measurement.target_index")
    ap.add_argument("--published-index", help="override the target's published_index for --arch")
    ap.add_argument("--consumed-index", action="append", dest="consumed_indexes", metavar="URL",
                    help="repository a consumer image enables next to the target (https:// "
                         "rpm-md or oci://…@sha256:…); overrides the target's "
                         "gap_measurement.consumed_indexes. Repeatable.")
    ap.add_argument("--desktop", action="append", dest="desktops")
    ap.add_argument("--cache", type=pathlib.Path, default=pathlib.Path(".cache/target-gap"))
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--baseline", type=pathlib.Path,
                    default=ROOT / "docs" / "hummingbird-installability-baseline.json",
                    help="recorded per-arch state the publish gate compares against")
    ap.add_argument("--fail-on-regression", action="store_true",
                    help="exit non-zero when a desktop loses a capability it "
                         "could resolve in the baseline (the publish gate)")
    ap.add_argument("--write-baseline", action="store_true",
                    help="record this measurement for --arch into --baseline, "
                         "leaving the other architectures untouched")
    ap.add_argument("--print-rebuild-names", action="store_true",
                    help="print one published package name per line: the "
                         "packages whose ABI the consumer universe does not "
                         "serve. A rebuild in the target's own root fixes "
                         "them and building more packages does not, so they "
                         "feed list-served-nvrs.py --except-names -- "
                         "otherwise the chain skips them as already served "
                         "and the leak outlives every wave.")
    ap.add_argument("--fail-on-unresolved", action="store_true")
    args = ap.parse_args(argv)

    factory = yaml.safe_load(args.factory.read_text(encoding="utf-8"))
    target = factory["targets"][args.target]
    catalog = yaml.safe_load(args.catalog.read_text(encoding="utf-8"))
    target_url = (args.target_index or target["gap_measurement"]["target_index"])
    target_url = target_url.replace("$arch", args.arch).replace("$basearch", args.arch)
    published_url = args.published_index or target["published_index"][args.arch]

    target_blob, target_prov = gap.primary_of(target_url, args.cache)
    published_blob, published_prov = gap.primary_of(published_url, args.cache)
    target_index = gap.parse_primary(target_blob)
    published_index = gap.parse_primary(published_blob)
    if args.consumed_indexes:
        measurement = {"consumed_indexes": [
            {"id": f"consumed-{i}", "index": url} for i, url in enumerate(args.consumed_indexes)]}
    else:
        measurement = target.get("gap_measurement") or {}
    consumed_meta = gap.consumed_indexes(measurement, args.arch, args.cache)
    consumed = [gap.parse_primary(gap.primary_of(
        c["baseurl"], args.cache, args.arch)[0]) for c in consumed_meta]
    for c in consumed_meta:
        c.pop("_have", None)
        c.pop("_have_evr", None)
    package_sets = args.desktops or [d for d in catalog["desktops"] if d != "bluefin"]
    if catalog.get("retained_packages") and RETAINED_SET not in package_sets:
        package_sets.append(RETAINED_SET)
    report = check(catalog, target_index, published_index, package_sets, consumed)
    out = {
        "measured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "arch": args.arch,
        "target_index": target_prov,
        "published_index": published_prov,
        "consumed_indexes": consumed_meta,
        "desktops": report,
        "rebuild": rebuild_list(report),
    }

    verdict = None
    if args.fail_on_regression or args.write_baseline:
        recorded = read_baseline(args.baseline)
        if args.fail_on_regression:
            arch_baseline = recorded.get("arches", {}).get(args.arch)
            if arch_baseline is None:
                print(f"{args.baseline}: no baseline recorded for {args.arch}; "
                      "refusing to call an unmeasured state a pass", file=sys.stderr)
                return 2
            verdict = compare(arch_baseline["desktops"], report)
            out["baseline_comparison"] = verdict
        if args.write_baseline:
            write_baseline(args.baseline, args.arch, out, report)

    if args.print_rebuild_names:
        # The chain's skip list is subtracted from this, so stdout carries
        # nothing else: a report line here would become a package name.
        for name in out["rebuild"]:
            print(name)
    elif args.as_json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"target    {target_url}  ({len(target_index['packages'])} binaries)")
        print(f"published {published_url}  ({len(published_index['packages'])} binaries)")
        for c in consumed_meta:
            print(f"consumed  {c['baseurl']}  ({c['binary_packages']} binaries, {c['id']})")
        print()
        print(render(report))
        if verdict is not None:
            print()
            print(f"**Against the baseline** (`{args.baseline.name}`, {args.arch}):")
            print(render_comparison(verdict))
    if verdict is not None and regressed(verdict):
        print("this measurement regresses the recorded installability baseline",
              file=sys.stderr)
        return 1
    if args.fail_on_unresolved and not all(r["resolvable"] for r in report.values()):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
