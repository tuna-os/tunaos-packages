#!/usr/bin/env python3
"""Generate docs/FACTORY-STATUS.md: what the factory has built, per target,
and what the catalog says is still needed.

RFC 011 Phase 1, first increment. Phase 0 gave the factory one catalog of
intent (manifests/catalog.yaml); until now the only way to answer "is this
package actually published for that target?" was to grep a primary.xml by
hand. This tool makes the answer a generated artifact with provenance:

  * for every target in manifests/package-factory.yaml that declares a
    `published_index` (the SERVED read URL(s) of its published repository —
    distinct from `r2_path`, the bucket write path), fetch every live
    rpm-md index it names and classify every catalog entry targeting it as
    BUILT (some binary or source package of that name is in one of them) or
    NEEDED. An arch may declare more than one index because a target can
    have more than one publisher (#467); BUILT is the union, because a
    buildroot pointed at that target sees all of them;
  * for EVERY target whose contract declares a gap_measurement roots
    manifest, additionally measure each desktop's `required_packages`
    roots against the index, per architecture — the coverage tables that
    decide which desktops tunaOS can wire (tunaOS#1755);
  * flat apt indexes (the tideforge deb repos) are read natively: Package:
    and Source: names from Packages.gz;
  * targets whose repository format this tool cannot read yet (pacman) are
    REPORTED as unmeasured with their entry counts — never silently
    dropped, per the no-silent-caps rule.

Everything is measured from live indexes; nothing is inferred from names or
memory. factory-status.json records the repomd revision and primary.xml
checksum every answer came from.

The full dependency-closure measurement (what a desktop transitively needs
that no repo supplies) remains scripts/gap_engine.py; its
target-parameterized generalization is the rest of RFC 011 Phase 1 and
arrives with the family-by-family build-order conversions.

Usage:
    scripts/factory-status.py                      # writes docs/ artifacts
    scripts/factory-status.py --check-structure    # no network: validates
                                                   # config shape for CI
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
import pathlib
import sys
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]

# repo.tunaos.org sits behind Cloudflare, which 403s python-urllib's default
# User-Agent while serving curl fine (measured 2026-08-18). Install a named
# opener globally so the fetcher imported below inherits it too.
_opener = urllib.request.build_opener()
_opener.addheaders = [(
    "User-Agent",
    "tunaos-factory-status/1 (+https://github.com/tuna-os/tunaos-packages)",
)]
urllib.request.install_opener(_opener)

# gap_engine.py owns the rpm-md machinery (fetch, repomd
# resolution, primary.xml parsing, srpm name derivation). Import it as a
# module rather than copying any of it — one implementation of index reading
# is the point of RFC 011.
sys.path.insert(0, str(ROOT / "scripts"))
import gap_engine as mhg  # noqa: E402  (needs the path above)
import published_index as pubidx  # noqa: E402


def load_yaml(path: pathlib.Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def index_names(baseurl: str, cache: pathlib.Path) -> tuple[set, dict]:
    """All names an index can answer for: binary names + source names."""
    blob, provenance = mhg.primary_of(baseurl, cache)
    parsed = mhg.parse_primary(blob)
    names = set(parsed["packages"])
    for info in parsed["packages"].values():
        source = mhg.srpm_name(info.get("srpm"))
        if source:
            names.add(source)
    provenance["package_names"] = len(parsed["packages"])
    return names, provenance


def apt_index_names(baseurl: str, cache: pathlib.Path) -> tuple[set, dict]:
    """All names a flat apt repo can answer for: Package: + Source: names.

    The tideforge deb repos are flat apt-ftparchive layouts (Packages.gz at
    the repo root — publish-tideforge-debs.yml). Source: may carry a
    ' (version)' suffix, which is stripped so the catalog's source names
    match.
    """
    base = baseurl.rstrip("/") + "/"
    raw = mhg.fetch(base + "Packages.gz", cache)
    text = gzip.decompress(raw).decode("utf-8", "replace")
    names: set = set()
    packages = 0
    for line in text.splitlines():
        if line.startswith("Package:"):
            names.add(line.split(":", 1)[1].strip())
            packages += 1
        elif line.startswith("Source:"):
            names.add(line.split(":", 1)[1].strip().split(" ", 1)[0])
    provenance = {
        "baseurl": base,
        "packages_gz_sha256": hashlib.sha256(raw).hexdigest(),
        "package_names": packages,
    }
    return names, provenance


def measure(catalog, factory, cache: pathlib.Path) -> dict:
    report = {
        "measured_at": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "targets": {},
        "unmeasured_targets": {},
    }
    entries = catalog["packages"]
    for target_id, target in factory["targets"].items():
        # set(): the same package can be catalogued by several families for
        # one target (e.g. cosmic-* by both the el10 COPR-replacement and
        # tideforge families); the question here is per NAME, not per entry.
        wanted = sorted({e["name"] for e in entries if target_id in e.get("targets", [])})
        published = target.get("published_index")
        if not published:
            report["unmeasured_targets"][target_id] = {
                "format": target.get("format"),
                "catalog_entries": len(wanted),
                "reason": "no published_index declared in package-factory.yaml",
            }
            continue
        readers = {"rpm": index_names, "deb": apt_index_names}
        reader = readers.get(target.get("format"))
        if reader is None:
            report["unmeasured_targets"][target_id] = {
                "format": target.get("format"),
                "catalog_entries": len(wanted),
                "reason": "only rpm-md and flat apt indexes are readable so far",
            }
            continue
        arches = {}
        for arch, declared in published.items():
            # An arch may declare several indexes (#467). BUILT is a union:
            # the question is whether a buildroot pointed at this target can
            # resolve the name, and it is pointed at all of them.
            names: set = set()
            provenances = []
            for url in pubidx.normalise(declared):
                found, provenance = reader(url, cache)
                names |= found
                provenances.append(provenance)
            built = sorted(n for n in wanted if n in names)
            needed = sorted(n for n in wanted if n not in names)
            arches[arch] = {
                "indexes": provenances,
                "catalog_entries": len(wanted),
                "built": built,
                "needed": needed,
            }
        report["targets"][target_id] = arches
    return report


# Kept small on purpose: counts per target/arch, one entry per merged
# measurement, capped. The full name lists live in each measurement's own
# commit; the history exists to answer "when did this number last MOVE".
HISTORY_LIMIT = 120


def _counts(report: dict) -> dict:
    return {
        f"{target_id}/{arch}": {
            "built": len(data["built"]),
            "needed": len(data["needed"]),
        }
        for target_id, arches in report.get("targets", {}).items()
        for arch, data in arches.items()
    }


def _days_between(older_iso: str, newer_iso: str) -> int:
    older = datetime.datetime.fromisoformat(older_iso)
    newer = datetime.datetime.fromisoformat(newer_iso)
    return max(0, (newer - older).days)


def add_trend(report: dict, previous: dict | None) -> None:
    """Make improvement itself a measured thing.

    The daily measurement used to OVERWRITE its predecessor, so "are we
    converging?" was answerable only by diffing git history by hand —
    and nothing alarmed when hummingbird published the same 570/262
    names for five straight days while its nightly quietly banked
    nothing (#480's broken resume). Three questions, answered in the
    artifact itself from the previously COMMITTED measurement:

      * what moved since last time (built/needed deltas, the names);
      * what REGRESSED — a name the catalog still asks for that was
        served and no longer is, which is the repo-wipe shape (#124)
        one measurement early. Names the CATALOG dropped are reported
        separately as `retired`: the target adopted them, so `built`
        falls with nothing lost, and folding the two together would
        make the detector alarm on its own success;
      * how long each unfinished target has gone without movement, from
        a compact per-measurement history the JSON now carries forward.

    The baseline is the last MERGED measurement, because the refresh
    workflow regenerates from main — so `staleness_days` also says how
    long refreshes have failed to land, which is itself a defect worth
    a loud line (#448 sat unmerged for five days and nothing said so).
    """
    history = list((previous or {}).get("history") or [])
    if previous and previous.get("measured_at"):
        entry = {"measured_at": previous["measured_at"],
                 "counts": _counts(previous)}
        if not history or history[-1]["measured_at"] != entry["measured_at"]:
            history.append(entry)
    report["history"] = history[-HISTORY_LIMIT:]

    if not previous or not previous.get("measured_at"):
        report["trend"] = None
        return

    trend = {
        "since": previous["measured_at"],
        "staleness_days": _days_between(previous["measured_at"],
                                        report["measured_at"]),
        "rows": {},
    }
    prev_targets = previous.get("targets", {})
    for target_id, arches in report["targets"].items():
        for arch, data in arches.items():
            key = f"{target_id}/{arch}"
            before = (prev_targets.get(target_id) or {}).get(arch)
            built_now = set(data["built"])
            if before is None:
                trend["rows"][key] = {"new": True,
                                      "built_delta": len(built_now),
                                      "needed_delta": len(data["needed"]),
                                      "newly_built": sorted(built_now),
                                      "regressed": [], "retired": []}
                continue
            built_before = set(before.get("built") or [])
            # BUILT is catalog n index, so a name leaves it for two very
            # different reasons: the index stopped serving it (#124), or the
            # CATALOG stopped asking for it because upstream adopted it --
            # RFC 011 success criterion 2, work removed. Subtracting the
            # second is the same subtraction the NEVER SHRINK baseline makes
            # for evicted foreign content: compare like against like, or the
            # detector cries wolf on its own success and nobody believes the
            # real wipe when it comes.
            catalog_now = built_now | set(data["needed"] or [])
            trend["rows"][key] = {
                "new": False,
                "built_delta": len(built_now) - len(built_before),
                "needed_delta": len(data["needed"]) - len(before.get("needed") or []),
                "newly_built": sorted(built_now - built_before),
                # Wanted then AND now, served then, absent now: the repo-wipe
                # shape, caught at measurement time instead of at a user's
                # failed install.
                "regressed": sorted((built_before & catalog_now) - built_now),
                # Served, and no longer asked for. Reported so the drop in
                # `built` is explained rather than unaccounted, but it is a
                # catalog shrinking, not an index losing content.
                "retired": sorted(built_before - catalog_now),
                "stalled": _stalled(report, key, len(built_now)),
            }
    report["trend"] = trend


def _stalled(report: dict, key: str, built_now: int) -> dict | None:
    """Days without movement, from the recorded history.

    None while the number just moved. When every recorded entry shows
    the same count, the stall is AT LEAST as old as the history —
    say so rather than inventing precision.
    """
    history = report.get("history") or []
    last_same = None
    for entry in reversed(history):
        counts = entry.get("counts", {}).get(key)
        if counts is None or counts.get("built") != built_now:
            break
        last_same = entry
    if last_same is None:
        return None
    days = _days_between(last_same["measured_at"], report["measured_at"])
    if days == 0:
        return None
    at_least = last_same is (history[0] if history else None)
    return {"days": days, "at_least": bool(at_least)}


def measure_desktop_coverage(report: dict, factory: dict,
                             cache: pathlib.Path) -> None:
    """Per-desktop required_packages coverage, for EVERY target that
    declares a roots manifest.

    The table that decides which desktops tunaOS can wire (tunaOS#1755).
    Driven by each target's `gap_measurement.roots_manifest` in the
    factory contract, never by a hard-coded target name — this section
    used to be hummingbird-only, which is how a target-neutral pipeline
    quietly grows one target's private reporting.
    """
    coverage: dict[str, dict] = {}
    for target_id, target in sorted(factory["targets"].items()):
        roots_manifest = (target.get("gap_measurement") or {}).get(
            "roots_manifest")
        measured = report["targets"].get(target_id)
        if not roots_manifest or not measured:
            continue
        manifest = load_yaml(ROOT / roots_manifest)
        desktops_spec = manifest.get("desktops") or {}
        if not desktops_spec:
            continue
        per_arch: dict[str, dict] = {}
        for arch, data in sorted(measured.items()):
            names: set = set()
            for provenance in data["indexes"]:
                found, _ = index_names(provenance["baseurl"], cache)
                names |= found
            desktops = {}
            for desktop, spec in sorted(desktops_spec.items()):
                roots = spec.get("required_packages", [])
                missing = sorted(r for r in roots if r not in names)
                desktops[desktop] = {
                    "roots": len(roots),
                    "present": len(roots) - len(missing),
                    "missing": missing,
                }
            per_arch[arch] = desktops
        if per_arch:
            coverage[target_id] = {"roots_manifest": roots_manifest,
                                   "architectures": per_arch}
    report["desktop_coverage"] = coverage


def _trim(names: list, limit: int = 6) -> str:
    if not names:
        return "—"
    shown = ", ".join(names[:limit])
    return shown if len(names) <= limit else f"{shown}, … ({len(names)} total)"


def render_trend(report: dict) -> list[str]:
    trend = report.get("trend")
    lines = ["## Progress since the last measurement", ""]
    if not trend:
        lines += ["First measurement on record — nothing to compare against",
                  "yet; the comparison begins when this one is committed.", ""]
        return lines
    lines.append(f"Compared against {trend['since']}, the last measurement")
    lines.append("that actually MERGED — so a large gap here means the daily")
    lines.append("refresh is not landing, which is its own defect:")
    lines.append("")
    if trend["staleness_days"] > 2:
        lines += [f"> **⚠ the previous measurement is {trend['staleness_days']}"
                  " days old.** The refresh PR is not merging; every delta",
                  "> below spans that whole window, and nothing in between",
                  "> was recorded.", ""]
    lines.append("| target/arch | built Δ | needed Δ | no movement for "
                 "| newly built |")
    lines.append("|---|---|---|---|---|")
    for key in sorted(trend["rows"]):
        row = trend["rows"][key]
        if row.get("new"):
            lines.append(f"| {key} | +{row['built_delta']} | "
                         f"+{row['needed_delta']} | newly measured | "
                         f"{_trim(row['newly_built'])} |")
            continue
        stalled = row.get("stalled")
        if stalled:
            movement = (f"≥ {stalled['days']} days" if stalled["at_least"]
                        else f"{stalled['days']} days")
        else:
            movement = "—"
        lines.append(
            f"| {key} | {row['built_delta']:+d} | {row['needed_delta']:+d} "
            f"| {movement} | {_trim(row['newly_built'])} |")
    lines.append("")
    regressions = {key: row["regressed"]
                   for key, row in trend["rows"].items() if row.get("regressed")}
    if regressions:
        lines.append("**REGRESSED — served by the previous measurement, "
                     "absent now.** This is the repo-wipe shape (#124) caught "
                     "at measurement time; treat it as an incident, not a "
                     "statistic:")
        lines.append("")
        for key, names in sorted(regressions.items()):
            lines.append(f"- {key}: {_trim(names, 12)}")
        lines.append("")
    retirements = {key: row["retired"]
                   for key, row in trend["rows"].items() if row.get("retired")}
    if retirements:
        lines.append("**Retired — built by the previous measurement, no "
                     "longer in the catalog.** The target adopted them, so "
                     "the factory stopped owing them; this is work REMOVED "
                     "(RFC 011 criterion 2), and it is why `built` can fall "
                     "without anything being lost:")
        lines.append("")
        for key, names in sorted(retirements.items()):
            lines.append(f"- {key}: {_trim(names, 12)}")
        lines.append("")
    return lines


def render(report: dict) -> str:
    lines = [
        "# Factory status — built vs needed, per target",
        "",
        "<!-- GENERATED by scripts/factory-status.py — do not hand-edit."
        " Regenerate: scripts/factory-status.py -->",
        "",
        f"Measured {report['measured_at']} from the live published indexes;",
        "provenance (repomd revision, primary.xml sha256) is in",
        "`docs/factory-status.json`, which also carries the full built and",
        "needed lists this page truncates.",
        "",
        "A catalog entry is **built** when the target's published index",
        "carries a binary or source package of that name, **needed** when it",
        "does not. This is presence, not freshness — version-level staleness",
        "needs catalog version pins, which most entries do not carry yet.",
        "",
    ]
    lines.extend(render_trend(report))
    for target_id, arches in sorted(report["targets"].items()):
        lines.append(f"## {target_id}")
        lines.append("")
        lines.append("| arch | catalog entries | built | needed | index packages |")
        lines.append("|---|---|---|---|---|")
        for arch, data in sorted(arches.items()):
            lines.append(
                f"| {arch} | {data['catalog_entries']} | {len(data['built'])} "
                f"| {len(data['needed'])} | "
                f"{sum(i['package_names'] for i in data['indexes'])} |"
            )
        lines.append("")
        for arch, data in sorted(arches.items()):
            needed = data["needed"]
            if not needed:
                continue
            shown = needed[:40]
            lines.append(
                f"Needed on {arch} ({len(needed)}"
                f"{'; first 40, full list in the JSON' if len(needed) > 40 else ''}):"
            )
            lines.append("")
            lines.append("```")
            lines.extend(shown)
            lines.append("```")
            lines.append("")
    for target_id, cov in (report.get("desktop_coverage") or {}).items():
        for arch, desktops in cov["architectures"].items():
            lines.append(f"## {target_id} desktop coverage ({arch})")
            lines.append("")
            lines.append(f"`required_packages` roots from {cov['roots_manifest']}")
            lines.append(
                "against the published index — the table that decides which"
            )
            lines.append("desktops tunaOS can wire (tunaOS#1755).")
            lines.append("")
            lines.append("| desktop | roots present | missing |")
            lines.append("|---|---|---|")
            for desktop, data in desktops.items():
                missing = ", ".join(data["missing"][:6])
                if len(data["missing"]) > 6:
                    missing += f", … ({len(data['missing'])} total)"
                lines.append(
                    f"| {desktop} | {data['present']}/{data['roots']} "
                    f"| {missing or '—'} |"
                )
            lines.append("")
    if report["unmeasured_targets"]:
        lines.append("## Not yet measured")
        lines.append("")
        lines.append(
            "These targets are NOT covered above — absence of a row is not"
        )
        lines.append("absence of a gap:")
        lines.append("")
        lines.append("| target | format | catalog entries | why |")
        lines.append("|---|---|---|---|")
        for target_id, data in sorted(report["unmeasured_targets"].items()):
            lines.append(
                f"| {target_id} | {data['format']} | {data['catalog_entries']} "
                f"| {data['reason']} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=pathlib.Path,
                        default=ROOT / "manifests" / "catalog.yaml")
    parser.add_argument("--factory", type=pathlib.Path,
                        default=ROOT / "manifests" / "package-factory.yaml")
    parser.add_argument("--out-md", type=pathlib.Path,
                        default=ROOT / "docs" / "FACTORY-STATUS.md")
    parser.add_argument("--out-json", type=pathlib.Path,
                        default=ROOT / "docs" / "factory-status.json")
    parser.add_argument("--cache", type=pathlib.Path,
                        default=pathlib.Path("/tmp/factory-status-cache"))
    parser.add_argument("--check-structure", action="store_true",
                        help="validate config shape without touching the network")
    args = parser.parse_args()

    catalog = load_yaml(args.catalog)
    factory = load_yaml(args.factory)

    if args.check_structure:
        measured = [t for t, spec in factory["targets"].items()
                    if spec.get("published_index")]
        for target_id in measured:
            spec = factory["targets"][target_id]
            if spec.get("format") not in ("rpm", "deb"):
                raise SystemExit(
                    f"{target_id}: published_index declared for a format "
                    "this tool cannot read yet"
                )
            for arch, declared in spec["published_index"].items():
                if arch not in spec.get("architectures", []):
                    raise SystemExit(
                        f"{target_id}: published_index arch {arch} is not in "
                        "the target's declared architectures"
                    )
                resolved = pubidx.normalise(declared)
                if not resolved:
                    raise SystemExit(
                        f"{target_id}/{arch}: published_index declared but empty"
                    )
                for url in resolved:
                    if not url.startswith(("https://", "file://")):
                        raise SystemExit(f"{target_id}/{arch}: unsupported URL {url}")
        if not measured:
            raise SystemExit("no target declares a published_index")
        print(f"structure ok: {len(measured)} measurable target(s)")
        return

    report = measure(catalog, factory, args.cache)
    measure_desktop_coverage(report, factory, args.cache)

    # The file this run is about to overwrite IS the last merged
    # measurement — the baseline that turns a snapshot into a trend.
    previous = None
    if args.out_json.exists():
        try:
            previous = json.loads(args.out_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            print(f"previous measurement unreadable ({error}); "
                  "treating this as the first", file=sys.stderr)
    add_trend(report, previous)
    trend = report.get("trend")
    if trend:
        for key in sorted(trend["rows"]):
            row = trend["rows"][key]
            marker = " REGRESSED: " + ", ".join(row["regressed"]) \
                if row.get("regressed") else ""
            print(f"{key}: built {row['built_delta']:+d} since "
                  f"{trend['since']}{marker}", file=sys.stderr)
        if trend["staleness_days"] > 2:
            print(f"::warning::previous factory-status measurement is "
                  f"{trend['staleness_days']} days old — the refresh PR is "
                  "not merging", file=sys.stderr)

    args.out_json.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.out_md.write_text(render(report), encoding="utf-8")
    for target_id, arches in sorted(report["targets"].items()):
        for arch, data in sorted(arches.items()):
            print(
                f"{target_id}/{arch}: {len(data['built'])} built, "
                f"{len(data['needed'])} needed of {data['catalog_entries']}",
                file=sys.stderr,
            )
    print(f"wrote {args.out_md} and {args.out_json}", file=sys.stderr)


if __name__ == "__main__":
    main()
