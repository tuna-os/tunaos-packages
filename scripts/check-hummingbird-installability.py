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
one names exactly what to build next.

Measured 2026-09-02 against the x86_64 indexes (docs/HUMMINGBIRD-TARGET.md):
the published prefix left 30 capabilities unresolved for the 58 GNOME roots,
three of them libm.so.6(GLIBC_2.44) -- a Rawhide-buildroot leak no amount of
further building would have fixed.

Usage:
  scripts/check-hummingbird-installability.py [--arch x86_64] [--desktop gnome ...]
      [--json] [--fail-on-unresolved]
  scripts/check-hummingbird-installability.py --published-index URL --target-index URL
      (defaults come from manifests/package-factory.yaml)
Exit status 0 unless --fail-on-unresolved and something is unresolved;
the report is the product.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import pathlib
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
            }
        report[package_set] = {
            "roots": len(roots),
            "roots_absent": sorted(absent),
            "closure_packages": len(reachable),
            "unresolved": detail,
            "resolvable": not absent and not unresolved,
        }
    return report


def render(report: dict) -> str:
    lines = ["| Package set | Roots | Absent roots | Closure | Unresolved capabilities | Resolvable |",
             "|---|---|---|---|---|---|"]
    for package_set, result in report.items():
        lines.append(f"| `{package_set}` | {result['roots']} | "
                     f"{len(result['roots_absent'])} | "
                     f"{result['closure_packages']} | {len(result['unresolved'])} | "
                     f"{'✅' if result['resolvable'] else '❌'} |")
    for package_set, result in report.items():
        if result["roots_absent"] or result["unresolved"]:
            lines.append("")
            lines.append(f"**{package_set}** — first blockers:")
            for name in result["roots_absent"][:10]:
                lines.append(f"- root `{name}`: in neither repository")
            for cap, detail in list(result["unresolved"].items())[:15]:
                lines.append(f"- `{cap}` needed by "
                             f"{', '.join(f'`{n}`' for n in detail['needed_by'][:3])} "
                             f"({'/'.join(detail['needer_from'])})")
    return "\n".join(lines)


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
    }
    if args.as_json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"target    {target_url}  ({len(target_index['packages'])} binaries)")
        print(f"published {published_url}  ({len(published_index['packages'])} binaries)")
        for c in consumed_meta:
            print(f"consumed  {c['baseurl']}  ({c['binary_packages']} binaries, {c['id']})")
        print()
        print(render(report))
    if args.fail_on_unresolved and not all(r["resolvable"] for r in report.values()):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
