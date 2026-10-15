#!/usr/bin/env python3
"""Turn "gnome 51 on hummingbird" into a plan the factory can execute.

## Why this exists

Every input the factory needs to build a desktop on a target is already
declared -- the target contract (`manifests/package-factory.yaml`), the roots
manifest it names, the cells in `manifests/package-builds.yaml`, and the gap
engine that generates the build order from live indexes. What did not exist
was a way to *say* the ask. Naming a desktop and a release meant knowing, and
hand-editing, every file that mentions the release track:

    manifests/hummingbird-desktops.yaml     desktops.<d>.release (+ .sources, or consumed_from)
    manifests/package-builds.yaml           each cell's source_paths
    manifests/catalog.yaml                  every entry's source path
    manifests/dependency-trees/gnome.yaml   the family's tree
    .github/workflows/build-chain-fanout.yml the epoch derivation paths
    .github/workflows/gap-drift.yml          the re-measure paths

Six files, and missing one is silent: an un-listed source path does not
re-key its cell, so the action cache serves output built from the OLD spec
and the change reads as applied when it was ignored (the defect
manifests/package-builds.yaml's own comments record for src/gnome-51 and
src/gnome-50 before it). So a release move is not a rename -- it is a
correctness-critical edit across six declarations, which is exactly the shape
that should be one command rather than six chances to be wrong.

## What a request resolves to

`resolve()` answers, from files only and with no network:

  * the target, and whether its contract can MEASURE a build order at all
    (a target with no `gap_measurement` block has a hand-curated order --
    el10 is the live example -- and that is reported, never papered over);
  * the desktop's roots, and whether the roots manifest already declares the
    requested release;
  * the architectures, cells, build order and served index URLs the wave
    will use;
  * every declaration that names the release track, so `adopt()` can move
    them together or a caller can show what a move would touch.

Nothing here builds, dispatches or measures against a live index. That is
deliberate: this module is the part that must be right offline, and
scripts/request.py is the CLI that adds the network-bound half.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
import typing

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
FACTORY = ROOT / "manifests" / "package-factory.yaml"
BUILDS = ROOT / "manifests" / "package-builds.yaml"


class RequestError(ValueError):
    """The ask cannot be resolved against the declared contract."""


# "gnome 51 on hummingbird", "gnome:51@hummingbird", "gnome@hummingbird",
# "gnome 51 hummingbird".  The release is optional: without one the roots
# manifest's own `release:` is the answer, which is how you ask for "whatever
# release this target is currently tracking".
_GRAMMAR = re.compile(
    r"""^\s*
    (?P<desktop>[a-z0-9][a-z0-9_+-]*)
    (?: \s* [:\s] \s* (?P<release>[0-9][0-9.]*|latest) )?
    \s* (?: @ | \s+on\s+ | \s+ ) \s*
    (?P<target>[a-z0-9][a-z0-9_.-]*)
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)


@dataclasses.dataclass(frozen=True)
class Declaration:
    """One place that names a release track, and how to rewrite it."""

    path: pathlib.Path
    # Number of occurrences of the current track at resolve time. Recorded so
    # a rewrite can assert it changed what it expected to change rather than
    # silently matching nothing.
    occurrences: int


@dataclasses.dataclass(frozen=True)
class Plan:
    request: str
    target: str
    desktop: str
    release: str
    # The release the roots manifest declares today. Differs from `release`
    # exactly when the request is asking for a MOVE.
    declared_release: str
    architectures: tuple[str, ...]
    roots_manifest: str
    build_order: str
    report_json: str
    drift_mode: str
    cells: tuple[str, ...]
    served_index: dict
    roots: tuple[str, ...]
    source_paths: tuple[str, ...]
    # Empty when the target's contract declares a gap_measurement; otherwise
    # the reason the build order cannot be generated from measurement.
    unmeasurable: str
    declarations: tuple[Declaration, ...]
    # Files a move touches that this tool refuses to rewrite: they model
    # more than one track and the shift is a decision, not a rename.
    decisions: tuple[Declaration, ...]
    notes: tuple[str, ...]

    @property
    def is_move(self) -> bool:
        # A target with no roots manifest declares no release, so there is
        # nothing to move FROM. Reporting a move there would invent a rename
        # out of an absence -- el10 is the live case.
        return bool(self.declared_release) and self.release != self.declared_release

    @property
    def track_dir(self) -> str:
        """The source tree a release track lives in: src/<desktop>-<release>."""
        return f"src/{self.desktop}-{self.release}"

    @property
    def declared_track_dir(self) -> str:
        return f"src/{self.desktop}-{self.declared_release}"

    def as_dict(self) -> dict:
        out = dataclasses.asdict(self)
        for field in ("declarations", "decisions"):
            out[field] = [
                {"path": str(d.path.relative_to(ROOT)),
                 "occurrences": d.occurrences}
                for d in getattr(self, field)
            ]
        out["is_move"] = self.is_move
        out["track_dir"] = self.track_dir
        return out


def parse(request: str) -> tuple[str, str | None, str]:
    """('gnome', '51', 'hummingbird') from any accepted spelling."""
    match = _GRAMMAR.match(request)
    if not match:
        raise RequestError(
            f"cannot read {request!r} as a request. Write it as "
            '"<desktop> <release> on <target>", e.g. "gnome 51 on hummingbird" '
            "(the release is optional)."
        )
    return (
        match["desktop"].lower(),
        match["release"],
        match["target"].lower(),
    )


def _load(path: pathlib.Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _cells_for(target: str, desktop: str, release: str,
               builds: dict) -> list[dict]:
    """The cells a wave for this (target, desktop) would run.

    Filtered by family when the target has one for this desktop, and only
    then. el10 carries four families on one target -- gnome50, gnome51, xfce,
    fprintd -- so an unfiltered list would dispatch xfce and fprintd for a
    gnome request. hummingbird carries one family (hummingbird-desktops)
    covering every desktop it declares, and filtering by name there would
    match nothing, so an empty family match falls back to the target's cells.
    """
    cells = [
        cell
        for cell in builds.get("native_builds", [])
        if cell.get("target") == target and cell.get("enabled", True)
    ]
    family_match = [
        cell for cell in cells
        if desktop in str(cell.get("family", "")).lower()
    ]
    if not family_match:
        return cells
    # A family may exist once per release track on the same target: el10
    # carries gnome50 and gnome51 side by side, each with its own manifest,
    # mock config and r2 prefix. `series:` is how a cell names its track, so
    # a request for one release must not dispatch the other's cells.
    series_match = [
        cell for cell in family_match
        if str(cell.get("series", "")) == release
    ]
    return series_match or family_match


def _served_index(contract: dict, arches: typing.Iterable[str]) -> dict:
    """Per-arch served read URL, from the same field factory-status reads.

    A missing arch is reported as None rather than dropped: an absent index
    is the honest answer for a prefix no wave has written yet, and a caller
    that silently skipped it would report "nothing served" as "nothing
    needed" -- the shape published_index's own tests exist to catch.
    """
    declared = contract.get("published_index") or {}
    out = {}
    for arch in arches:
        value = declared.get(arch)
        if isinstance(value, list):
            out[arch] = list(value)
        elif value:
            out[arch] = [value]
        else:
            out[arch] = []
    return out


def _sources_of(definition: dict) -> list[str]:
    """The `local:` source paths a desktop declares, in order."""
    paths = []
    for source in definition.get("sources", []) or []:
        if isinstance(source, dict) and "local" in source:
            paths.append(source["local"])
    return paths


# Every file that names a release track, in one of three categories. The
# categories exist because "move gnome 51 to 52" is not one operation:
#
#   MECHANICAL     the track is a path to the source tree and nothing else.
#                  Every occurrence moves, and missing one is SILENT -- an
#                  un-listed source_path does not re-key its cell, so the
#                  action cache serves output built from the old spec and the
#                  change reads as applied when it was ignored. That defect is
#                  recorded in manifests/package-builds.yaml's own comments,
#                  for src/gnome-51 and src/gnome-50 in turn.
#
#   DECIDED        the file models MORE than one track at once.
#                  manifests/dependency-trees/gnome.yaml carries `stable:` and
#                  `next:` rows; a real release move shifts both (50->51 and
#                  51->52), and rewriting only the row that matches the
#                  requested track leaves a table claiming 50 is stable while
#                  52 is next. So it is reported and never rewritten.
#
#   HISTORICAL     the track appears in prose recording what once happened --
#                  #542's downgrade of nineteen packages from src/gnome-51 to
#                  src/gnome-50, and the comments explaining why the code
#                  guards against it. Rewriting those would make a comment
#                  describe a move that did not happen, which is worse than a
#                  stale comment.
#
# tests/test_a_release_move_touches_every_declaration.py asserts every tracked
# file naming a track is in exactly one category, so a new declaration cannot
# be added without being classified.
TRACK_DECLARATIONS = (
    # Still here after gnome became `consumed_from: utah-packages` (#629):
    # the catalog's source_paths keep naming the src/gnome-51 tree, because
    # what utah does NOT ship (gjs, nautilus, gnome-initial-setup, ... --
    # 8 packages on 2026-09-02) is still built from those reviewed specs
    # for the other desktops.
    "manifests/hummingbird-desktops.yaml",
    "manifests/package-builds.yaml",
    "manifests/catalog.yaml",
    ".github/workflows/build-chain-fanout.yml",
    # renovate.json pins 22 `fileMatch` patterns to ^src/gnome-51/... A move
    # that left them behind would keep Renovate watching a dead tree and stop
    # it watching the live one -- which is not a hypothetical: renovate.json's
    # own comment records that src/gnome-51 forked from src/gnome-50 without
    # carrying its Renovate coverage, so "gtk4/libadwaita/gnome-shell etc.
    # drifted silently until #580 caught it by hand". That is this table's
    # failure mode, found the expensive way, on this very file.
    "renovate.json",
)

TRACK_DECISIONS = (
    "manifests/dependency-trees/gnome.yaml",
    # README's layout table lists every GNOME source tree side by side
    # (src/gnome-49, -50, -51) plus a per-track build order. Rewriting only
    # the requested track's name would leave it listing 49/50/52 with no 51,
    # so a move reports it for a human to update and never rewrites it.
    "README.md",
)

TRACK_HISTORY = (
    ".github/workflows/gap-drift.yml",
    "scripts/gap_engine.py",
    "scripts/summarize-gap-drift.py",
    "scripts/verify-package-factory-cell.sh",
    # This module and the classifier name the current track in their own
    # prose -- the defect package-builds.yaml records, #542's downgrade, a
    # worked example of a chain summary. Classified rather than reworded so
    # the explanation keeps naming the real case: an example that has been
    # made generic to satisfy a test explains less than the case it came
    # from.
    "scripts/build_request.py",
    "scripts/classify-chain-failures.py",
)


def _declarations(track: str) -> tuple[Declaration, ...]:
    found = []
    for relative in TRACK_DECLARATIONS:
        path = ROOT / relative
        if not path.exists():
            continue
        count = path.read_text(encoding="utf-8").count(track)
        if count:
            found.append(Declaration(path=path, occurrences=count))
    return tuple(found)


def _decisions(track: str) -> tuple[Declaration, ...]:
    found = []
    for relative in TRACK_DECISIONS:
        path = ROOT / relative
        if not path.exists():
            continue
        count = path.read_text(encoding="utf-8").count(track)
        if count:
            found.append(Declaration(path=path, occurrences=count))
    return tuple(found)


def resolve(request: str, factory_path: pathlib.Path = FACTORY,
            builds_path: pathlib.Path = BUILDS) -> Plan:
    desktop, asked_release, target = parse(request)
    factory = _load(factory_path)
    targets = factory.get("targets") or {}
    if target not in targets:
        raise RequestError(
            f"{target}: not a target in {factory_path.name}. "
            f"Declared targets: {', '.join(sorted(targets))}."
        )
    contract = targets[target]
    notes: list[str] = []

    measurement = contract.get("gap_measurement") or {}
    unmeasurable = ""
    if not measurement:
        unmeasurable = (
            f"{target} declares no gap_measurement, so its build order is "
            "curated by hand and cannot be regenerated from the live indexes. "
            "Adding a gap_measurement block (roots manifest + target, "
            "reference and source-reference indexes) is what makes this "
            "target answerable to a request."
        )

    roots_manifest = measurement.get("roots_manifest", "")
    drift = measurement.get("drift") or {}
    build_order = drift.get("build_order", "")
    report_json = drift.get("report_json", "")
    drift_mode = drift.get("mode", "")

    roots: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()
    declared_release = ""
    if roots_manifest:
        catalog_path = ROOT / roots_manifest
        if not catalog_path.exists():
            raise RequestError(
                f"{roots_manifest}: declared by {target}'s gap_measurement "
                "but not in the tree"
            )
        catalog = _load(catalog_path)
        desktops = catalog.get("desktops") or {}
        if desktop not in desktops:
            raise RequestError(
                f"{desktop}: not a desktop in {roots_manifest}. "
                f"Declared: {', '.join(sorted(desktops))}."
            )
        definition = desktops[desktop]
        declared_release = str(definition.get("release", ""))
        roots = tuple(
            dict.fromkeys(
                list(definition.get("required_packages") or [])
                + list(definition.get("install_packages") or [])
            )
        )
        source_paths = tuple(_sources_of(definition))
    elif asked_release is None:
        raise RequestError(
            f"{target} has no roots manifest to read a release from, so the "
            "request must name one explicitly."
        )

    release = asked_release or declared_release
    if not release:
        raise RequestError(
            f"{desktop} on {target}: neither the request nor "
            f"{roots_manifest} names a release."
        )

    if declared_release and release != declared_release:
        track = ROOT / f"src/{desktop}-{release}"
        if not track.is_dir():
            notes.append(
                f"{desktop} {release} has no source tree at src/{desktop}-"
                f"{release}. A move needs one -- import it (or branch it from "
                f"src/{desktop}-{declared_release}) before adopting."
            )
        notes.append(
            f"{roots_manifest} declares {desktop} {declared_release}; this "
            f"request asks for {release}. That is a MOVE across "
            f"{len(_declarations(f'src/{desktop}-{declared_release}'))} "
            "declarations -- see `request.py --adopt`."
        )

    cells = _cells_for(target, desktop, release, _load(builds_path))
    arches = tuple(
        dict.fromkeys(
            [cell["architecture"] for cell in cells]
            or list(contract.get("architectures") or [])
        )
    )
    if not cells:
        notes.append(
            f"{target} has no build-chain cell in {builds_path.name}; a wave "
            "cannot be dispatched for it until one is declared."
        )

    return Plan(
        request=request,
        target=target,
        desktop=desktop,
        release=release,
        declared_release=declared_release,
        architectures=arches,
        roots_manifest=roots_manifest,
        build_order=build_order,
        report_json=report_json,
        drift_mode=drift_mode,
        cells=tuple(cell["id"] for cell in cells),
        served_index=_served_index(contract, arches),
        roots=roots,
        source_paths=source_paths,
        unmeasurable=unmeasurable,
        declarations=_declarations(f"src/{desktop}-{declared_release}")
        if declared_release else (),
        decisions=_decisions(f"src/{desktop}-{declared_release}")
        if declared_release else (),
        notes=tuple(notes),
    )


def adopt(plan: Plan, *, apply: bool = False) -> dict[str, int]:
    """Move every declaration from the declared track to the requested one.

    Returns {path: replacements}. Raises when a declaration the plan recorded
    yields a different count than it recorded -- a rewrite that silently
    matched nothing, or matched more than it read, is the failure this whole
    table exists to prevent.

    Files in TRACK_DECISIONS are never rewritten; the caller is expected to
    surface plan.decisions so the human making the release decision knows what
    is still theirs to change.
    """
    if not plan.is_move:
        return {}
    old, new = plan.declared_track_dir, plan.track_dir
    changed: dict[str, int] = {}
    for declaration in plan.declarations:
        text = declaration.path.read_text(encoding="utf-8")
        count = text.count(old)
        if count != declaration.occurrences:
            raise RequestError(
                f"{declaration.path}: expected {declaration.occurrences} "
                f"occurrences of {old}, found {count}; refusing a partial "
                "rewrite"
            )
        if apply:
            declaration.path.write_text(text.replace(old, new), encoding="utf-8")
        try:
            key = str(declaration.path.relative_to(ROOT))
        except ValueError:
            # A caller may point declarations at a copy of the tree (tests
            # do). Reporting the path it actually rewrote beats raising over
            # where that path happens to live.
            key = str(declaration.path)
        changed[key] = count
    return changed
