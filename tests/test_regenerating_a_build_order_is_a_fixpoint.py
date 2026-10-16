"""Regenerating a build order must not move a package to a different spec dir.

The gap engine picks each package's spec directory by walking the roots
manifest's `source_paths:` in order and taking the first prefix that has a
directory of that name (scripts/gap_engine.py, `locate`). So `source_paths`
is the only thing deciding which GNOME track hummingbird builds -- and it is
NOT the file anyone edits when they repoint a track.

Measured on 2026-08-26, before this test existed:

    build-order-hummingbird-desktops.yml   19 packages from src/gnome-51/
    manifests/hummingbird-desktops.yaml    source_paths: [src/gnome-50, ...]

#521 repointed the build order to src/gnome-51 and updated the CELL's
source_paths in manifests/package-builds.yaml, which is what keys the cache.
It did not update the roots manifest, which is what regenerates the order.
Both files are named `source_paths` and they are different keys.

The consequence is not a red build. hummingbird's drift mode is `propose`
(manifests/package-factory.yaml): a scheduled job regenerates the order when
the live target index moves and opens a PR. That PR would have carried
gdm, gnome-shell, mutter, gtk4, libadwaita and 14 more from src/gnome-51
back to src/gnome-50 -- a silent downgrade of the whole desktop, arriving
under a title about re-measuring the gap.

The property is a fixpoint: for every `path:` the checked-in order already
names, resolving that name through the roots manifest must return that same
path. Anything else means a regeneration would rewrite the file.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
FACTORY = yaml.safe_load(
    (ROOT / "manifests" / "package-factory.yaml").read_text(encoding="utf-8"))


def _cases():
    for name, target in sorted(FACTORY["targets"].items()):
        gap = target.get("gap_measurement") or {}
        roots = gap.get("roots_manifest")
        order = (gap.get("drift") or {}).get("build_order")
        if roots and order and (ROOT / roots).is_file() \
                and (ROOT / order).is_file():
            yield name, roots, order


CASES = list(_cases())


def referenced_paths(order: str) -> set[str]:
    """Every `path:` under src/ the build order names."""
    spec = yaml.safe_load((ROOT / order).read_text(encoding="utf-8"))
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            path = node.get("path")
            if isinstance(path, str) and path.startswith("src/"):
                found.add(path.rstrip("/"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(spec)
    return found


def resolver(roots: str):
    """gap_engine.emit_build_order's `locate`, reproduced from the manifest."""
    catalog = yaml.safe_load((ROOT / roots).read_text(encoding="utf-8"))
    search = catalog["source_paths"]
    fallback = catalog["distgit_prefix"]
    always_distgit = set(catalog.get("prefer_distgit") or [])

    def locate(name: str) -> str:
        if name not in always_distgit:
            for prefix in search:
                if (ROOT / prefix / name).is_dir():
                    return f"{prefix}/{name}"
        return f"{fallback}/{name}"

    return locate


def test_there_are_targets_to_check():
    """A rename of gap_measurement would make every case below vacuous."""
    assert CASES, "no target declares both a roots_manifest and a build_order"


@pytest.mark.parametrize("target,roots,order", CASES,
                         ids=[c[0] for c in CASES])
def test_regeneration_would_not_move_any_package(target, roots, order):
    locate = resolver(roots)
    moved = sorted(
        (path, locate(path.rsplit("/", 1)[1]))
        for path in referenced_paths(order)
        if locate(path.rsplit("/", 1)[1]) != path
    )
    assert not moved, (
        f"{target}: regenerating {order} from {roots} would move these "
        f"packages to a different spec directory, so a scheduled re-measure "
        f"silently rewrites what gets built. Fix `source_paths:` in {roots} "
        f"(it is a different key from the cell's source_paths in "
        f"manifests/package-builds.yaml): {moved}")


@pytest.mark.parametrize("target,roots,order", CASES,
                         ids=[c[0] for c in CASES])
def test_a_declared_desktop_source_is_a_prefix_the_resolver_searches(
        target, roots, order):
    """`desktops.<d>.sources` naming a local dir outside `source_paths` is a
    track the manifest claims to build and the resolver can never reach.

    A local source is either a searched tree (src/xfce-wayland) or one
    package's spec directory directly inside one (src/hummingbird/
    tuna-desktop, built in local-00)."""
    catalog = yaml.safe_load((ROOT / roots).read_text(encoding="utf-8"))
    search = [p.rstrip("/") for p in catalog["source_paths"]]
    unreachable = sorted(
        (desktop, source["local"].rstrip("/"))
        for desktop, spec in (catalog.get("desktops") or {}).items()
        for source in spec.get("sources", [])
        if "local" in source
        and source["local"].rstrip("/") not in search
        and source["local"].rstrip("/").rsplit("/", 1)[0] not in search
    )
    assert not unreachable, (
        f"{target}: these desktops declare a local source directory that "
        f"`source_paths: {search}` never searches, so nothing is built from "
        f"it: {unreachable}")


def test_the_fixpoint_check_can_actually_fail():
    """Against a resolver that always agrees, the test above passes for the
    wrong reason. Point hummingbird's search at the track it was on before
    this commit and the GNOME packages still built from src/gnome-51 must be
    reported as moving.

    That set was 19 until 2026-09-02; it is 8 now (gjs, nautilus,
    gnome-initial-setup, gobject-introspection, vte291, ptyxis,
    gnome-online-accounts and a libnotify), because gnome-shell, gdm and the
    rest of the stack are consumed from utah-packages (#629) and no longer
    appear in the order at all. The hazard is unchanged: every one of those
    8 has a src/gnome-50 twin the stale search would silently prefer."""
    order = "build-order-hummingbird-desktops.yml"
    paths = referenced_paths(order)
    assert paths, "the walker found no paths at all"

    search = ["src/gnome-50", "src/deps", "src/hummingbird", "src/xfce-wayland"]

    def stale(name):
        for prefix in search:
            if (ROOT / prefix / name).is_dir():
                return f"{prefix}/{name}"
        return f"src/hummingbird/{name}"

    moved = [p for p in paths if stale(p.rsplit("/", 1)[1]) != p]
    assert len(moved) >= 5, (
        "searching src/gnome-50 first moved fewer packages than the 8 "
        f"measured, so this check is not exercising the real hazard: {moved}")
    assert any(p.endswith("/nautilus") for p in moved), moved
    assert any(p.endswith("/gjs") for p in moved), moved


def test_prefer_distgit_names_a_real_collision():
    """A `prefer_distgit:` entry with no directory shadowing it is dead
    config -- and the day someone adds that directory, it looks intentional
    while doing nothing."""
    for target, roots, _order in CASES:
        catalog = yaml.safe_load((ROOT / roots).read_text(encoding="utf-8"))
        for name in catalog.get("prefer_distgit") or []:
            shadowing = [p for p in catalog["source_paths"]
                         if (ROOT / p / name).is_dir()]
            assert shadowing, (
                f"{target}: prefer_distgit lists {name!r}, but no directory "
                f"under source_paths {catalog['source_paths']} shadows it, "
                f"so the entry changes nothing")


def test_the_engine_itself_honours_prefer_distgit():
    """The resolver above is a reproduction of gap_engine.locate. If only the
    reproduction learned `prefer_distgit`, every check in this file passes
    while the real regeneration still downgrades. Drive the real emitter."""
    import importlib.util
    import tempfile

    spec = importlib.util.spec_from_file_location(
        "gap_engine", ROOT / "scripts" / "gap_engine.py")
    gap_engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gap_engine)

    catalog = {
        "target": {"id": "t-1", "r2_path": "t/1"},
        "source_paths": ["src/deps"],
        "distgit_prefix": "src/hummingbird",
    }
    report = {
        "measured_at": "2026-01-01T00:00:00+00:00",
        "target_index": {"primary_sha256": "0" * 8},
        "reference_index": {"primary_sha256": "0" * 8},
    }
    tiers = [["libnotify"]]  # global_tiers is a list of name lists

    def emit(cat):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "order.yml"
            gap_engine.emit_build_order(out, cat, tiers, [], report, ROOT)
            return out.read_text(encoding="utf-8")

    without = emit(catalog)
    assert "src/deps/libnotify" in without, (
        "src/deps/libnotify no longer shadows the import, so this test is "
        f"not exercising the hazard:\n{without}")

    with_pin = emit({**catalog, "prefer_distgit": ["libnotify"]})
    assert "src/hummingbird/libnotify" in with_pin, with_pin
    assert "src/deps/libnotify" not in with_pin, with_pin
