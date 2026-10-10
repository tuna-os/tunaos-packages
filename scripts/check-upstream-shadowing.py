#!/usr/bin/env python3
"""Which packages we still serve are OLDER than the build upstream now ships.

Hummingbird exists to ship fixes as soon as they are published. Our rebuild
repository can defeat that, and silently.

The gap measurement generates the build set as (desktop closure) minus (what
upstream ships), so the moment upstream adopts a package it drops out of the
BUILD ORDER and we stop rebuilding it. Nothing, however, withdraws the copy we
already published. The build set shrinks; the served repository does not. Our
copy stays indexed at whatever version it was cut at, forever.

That is not merely dead weight. The desktop manifests add our repository with

    {name: tunaos-hummingbird, baseurl: .../hummingbird/<snapshot>-<arch>/,
     priority: 5}

and dnf's `priority` is ABSOLUTE, not a tie-break: a package from a
higher-priority repository wins even when a lower-priority one offers a newer
build. Upstream's repository carries no such priority. So for any name present
in both, ours is chosen -- and if ours is older, upstream's fix is shadowed by
our stale rebuild on every image built from that repository.

Measured on 2026-08-25 against the two live indexes:

    ours 7986 binary names, upstream 3509, overlap 30
    16 of those 30 are OLDER on our side, including

        sudo             1.9.17-1.p2.fc43   <-  1.9.17-16.p2.hum1
        libxkbcommon     1.13.1-3.fc43      <-  1.13.1-3.hum1
        xkeyboard-config 2.48-1.fc43        <-  2.48-4.hum1

`sudo` fifteen releases behind, on a distribution whose entire premise is a
zero-CVE catalogue. This check is what makes that state fail a run instead of
sitting unnoticed in an index nobody diffs.

The remedy is to WITHDRAW ours, not to rebuild it: upstream shipping the
package is precisely why the gap measurement stopped asking us to build it.
Withdrawal is a publish-side action and this script deliberately does not
perform it -- it names what to withdraw and exits non-zero.

Generic over targets on purpose. Both halves are already declared per target
in manifests/package-factory.yaml (`r2_path` and
`gap_measurement.target_index`), so a second target opting in needs no code
change. The previous reactive driver named hummingbird in its filename and its
outputs, which is exactly why removing hummingbird specifics removed the
reactivity with it (#517).

Usage:
    scripts/check-upstream-shadowing.py
    scripts/check-upstream-shadowing.py --target hummingbird
    scripts/check-upstream-shadowing.py --report-json /tmp/shadowing.json
    scripts/check-upstream-shadowing.py --warn-only
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SERVED_ROOT = "https://repo.tunaos.org/"

import gap_engine  # noqa: E402
import rpm_vercmp  # noqa: E402


def arch_of(r2_path: str, override: str | None = None) -> str:
    """The architecture a served prefix holds.

    Derived from the prefix rather than assumed: r2_path is
    `<name>/<snapshot>-<arch>`, and getting this wrong would compare x86_64
    against aarch64 and report every package as shadowed.
    """
    if override:
        return override
    m = re.search(r"-(x86_64|aarch64|i686|ppc64le|s390x)$", r2_path.rstrip("/"))
    if not m:
        raise ValueError(
            f"cannot derive an architecture from r2_path {r2_path!r}; "
            "pass --arch"
        )
    return m.group(1)


def targets_to_check(factory: dict, only: str | None = None) -> list[tuple[str, dict]]:
    """Targets declaring both a served prefix and an upstream index."""
    out = []
    for name, target in (factory.get("targets") or {}).items():
        if only and name != only:
            continue
        gap = target.get("gap_measurement") or {}
        if target.get("r2_path") and gap.get("target_index"):
            out.append((name, target))
    return out


def shadowed(served: dict, upstream: dict, compare_evr) -> list[dict]:
    """Names present in both indexes where OURS is the older build.

    Compares binary packages only; a source rpm in either index describes
    what built a package rather than what an image installs.
    """
    found = []
    for name, ours in served.items():
        theirs = upstream.get(name)
        if theirs is None:
            continue
        if ours.get("arch") == "src" or theirs.get("arch") == "src":
            continue
        if compare_evr(ours["evr"], theirs["evr"]) < 0:
            found.append({
                "package": name,
                "served_evr": ours["evr"],
                "upstream_evr": theirs["evr"],
            })
    return sorted(found, key=lambda row: row["package"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="only this target (default: all declared)")
    ap.add_argument("--arch", help="override the architecture derived from r2_path")
    ap.add_argument("--report-json")
    ap.add_argument("--warn-only", action="store_true",
                    help="report and exit 0")
    ap.add_argument("--cache", default="/tmp/tunaos-index-cache")
    args = ap.parse_args(argv)

    cache = pathlib.Path(args.cache)

    factory = yaml.safe_load((ROOT / "manifests" / "package-factory.yaml").read_text())
    selected = targets_to_check(factory, args.target)
    if not selected:
        print(f"ERROR: no target declares both r2_path and "
              f"gap_measurement.target_index"
              + (f" (--target {args.target})" if args.target else ""),
              file=sys.stderr)
        return 2

    report = {"targets": {}}
    total = 0
    unreadable = 0

    for name, target in selected:
        r2_path = target["r2_path"]
        arch = arch_of(r2_path, args.arch)
        served_url = SERVED_ROOT + r2_path.strip("/") + "/"
        upstream_url = (target["gap_measurement"]["target_index"]
                        .replace("$arch", arch).replace("$basearch", arch))
        try:
            served_blob, served_prov = gap_engine.primary_of(served_url, cache)
            upstream_blob, upstream_prov = gap_engine.primary_of(upstream_url, cache)
        except Exception as exc:
            # An unreadable index must never read as "nothing is shadowed".
            # That is the same failure mode the drift detector guards: a
            # silent green on a measurement that did not happen.
            print(f"::warning::{name}: could not read an index ({exc})")
            unreadable += 1
            continue

        served = gap_engine.parse_primary(served_blob)["packages"]
        upstream = gap_engine.parse_primary(upstream_blob)["packages"]
        rows = shadowed(served, upstream, rpm_vercmp.compare_evr)
        total += len(rows)

        report["targets"][name] = {
            "served_url": served_url,
            "served_revision": served_prov.get("revision"),
            "upstream_url": upstream_url,
            "upstream_revision": upstream_prov.get("revision"),
            "served_names": len(served),
            "upstream_names": len(upstream),
            "shadowed": rows,
        }

        print(f"{name}: {len(served)} served, {len(upstream)} upstream, "
              f"{len(rows)} shadowed")
        for row in rows:
            print(f"  {row['package']:32s} served {row['served_evr']}  "
                  f"< upstream {row['upstream_evr']}")

    if args.report_json:
        pathlib.Path(args.report_json).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n")

    if total:
        print(f"\nERROR: {total} package(s) are served at an older build than "
              f"upstream now ships. Our repository is added at priority 5, and "
              f"dnf priority is absolute, so these shadow upstream's builds "
              f"rather than losing to them. Withdraw them from the published "
              f"repository -- upstream shipping them is why the gap "
              f"measurement no longer asks us to build them.",
              file=sys.stderr)
        if not args.warn_only:
            return 1
    if unreadable:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
