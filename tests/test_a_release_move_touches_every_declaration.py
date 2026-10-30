"""Moving a release track is six edits, and missing one is silent.

`src/gnome-51` is named in eight tracked files. Four are paths to the source
tree, one is a two-track table, three are prose recording history. A request
for "gnome 52 on hummingbird" has to move the first four together, report the
fifth, and leave the last three alone.

Why the silence matters more than the count. manifests/package-builds.yaml
carries the cell's `source_paths`, and its own comments record what happens
when one is missing:

    src/gnome-51/ is here because build-order-hummingbird-desktops.yml pulls
    19 packages from it ... Without the path listed, an edit to any of those
    specs does not move this cell's action key, so the cache restores an
    ActionResult built from the OLD spec and the change is silently ignored.

So a partial move can silently build and ship the previous release.
"""
from __future__ import annotations

import pathlib
import shutil
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import build_request as br  # noqa: E402


def live_tracks() -> set[str]:
    """The source trees a request could actually move today.

    Only the tracks a gap-measured target DECLARES, because those are the only
    ones `--adopt` ever rewrites. src/gnome-50 is el10's stable track and no
    request moves it, so a file naming it is not evidence of a stale table --
    demanding it be classified would make this test fail on unrelated prose
    and teach the next reader to widen the exemptions instead of the table.
    """
    factory = yaml.safe_load(
        (REPO / "manifests" / "package-factory.yaml").read_text()
    )
    tracks: set[str] = set()
    for target in factory["targets"].values():
        measurement = target.get("gap_measurement") or {}
        roots = measurement.get("roots_manifest")
        if not roots:
            continue
        catalog = yaml.safe_load((REPO / roots).read_text())
        for desktop, definition in (catalog.get("desktops") or {}).items():
            release = str(definition.get("release", ""))
            candidate = f"src/{desktop}-{release}"
            if release and (REPO / candidate).is_dir():
                tracks.add(candidate)
    return tracks


def test_there_is_a_live_track_to_move() -> None:
    """Everything below is vacuous without one."""
    assert live_tracks(), (
        "no gap-measured target declares a desktop whose src/<desktop>-"
        "<release> tree exists; the move guard has nothing to guard"
    )




def test_no_file_is_in_two_categories() -> None:
    """A file cannot be both rewritten and left alone."""
    names = (
        list(br.TRACK_DECLARATIONS)
        + list(br.TRACK_DECISIONS)
        + list(br.TRACK_HISTORY)
    )
    assert len(names) == len(set(names)), sorted(
        n for n in names if names.count(n) > 1
    )


@pytest.mark.parametrize(
    "relative", br.TRACK_DECLARATIONS + br.TRACK_DECISIONS + br.TRACK_HISTORY
)
def test_every_classified_file_still_names_a_track(relative: str) -> None:
    """A classification for a file that no longer mentions a track is dead.

    Dead entries are how the table looks complete while covering nothing: an
    `--adopt` reads six names, rewrites two, and reports success.
    """
    path = REPO / relative
    assert path.exists(), relative
    text = path.read_text(encoding="utf-8")
    assert any(track in text for track in live_tracks()), (
        f"{relative} is classified as naming a live release track and does not"
    )


def test_the_mechanical_files_carry_the_source_paths_a_cell_keys_on() -> None:
    """The one that must never be missed, asserted by its consequence."""
    assert "manifests/package-builds.yaml" in br.TRACK_DECLARATIONS
    text = (REPO / "manifests" / "package-builds.yaml").read_text()
    assert "src/gnome-51/" in text
    # Both hummingbird cells list it; a move that touched one would re-key one
    # arch and serve the other from cache.
    assert text.count("src/gnome-51/") >= 2


def test_adopt_moves_all_of_them_or_none(tmp_path: pathlib.Path) -> None:
    """Run a real move against a copy of the tree, then check every file."""
    work = tmp_path / "repo"
    work.mkdir()
    for relative in (
        br.TRACK_DECLARATIONS + br.TRACK_DECISIONS
        + ("manifests/package-factory.yaml",)
    ):
        destination = work / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / relative, destination)

    plan = br.resolve(
        "gnome 52 on hummingbird",
        factory_path=work / "manifests" / "package-factory.yaml",
        builds_path=work / "manifests" / "package-builds.yaml",
    )
    assert plan.is_move
    # resolve() reads declarations from the real tree; point them at the copy.
    moved = br.Plan(
        **{
            **{f.name: getattr(plan, f.name) for f in
               __import__("dataclasses").fields(plan)},
            "declarations": tuple(
                br.Declaration(path=work / d.path.relative_to(REPO),
                               occurrences=d.occurrences)
                for d in plan.declarations
            ),
        }
    )
    changed = br.adopt(moved, apply=True)
    assert len(changed) == len(br.TRACK_DECLARATIONS), changed

    for relative in br.TRACK_DECLARATIONS:
        text = (work / relative).read_text()
        assert "src/gnome-51" not in text, f"{relative} kept the old track"
        assert "src/gnome-52" in text, f"{relative} did not get the new track"
    # The two-track table is a decision, not a rename: adopt must not touch it.
    for relative in br.TRACK_DECISIONS:
        assert "src/gnome-51" in (work / relative).read_text(), (
            f"{relative} was rewritten; a two-track table's shift is a "
            "decision the tool must leave to a human"
        )


def test_a_declaration_that_moved_under_us_stops_the_rewrite(
    tmp_path: pathlib.Path,
) -> None:
    """A partial rewrite is worse than none, so a count mismatch raises."""
    target = tmp_path / "manifests" / "hummingbird-desktops.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("src/gnome-51\n")
    plan = br.resolve("gnome 52 on hummingbird")
    stale = br.Plan(
        **{
            **{f.name: getattr(plan, f.name) for f in
               __import__("dataclasses").fields(plan)},
            # Claims two occurrences; the file has one.
            "declarations": (br.Declaration(path=target, occurrences=2),),
        }
    )
    with pytest.raises(br.RequestError, match="refusing a partial rewrite"):
        br.adopt(stale, apply=True)
    assert target.read_text() == "src/gnome-51\n", "the file was rewritten anyway"


def test_renovate_follows_the_track_it_is_meant_to_watch() -> None:
    """A move that leaves Renovate behind stops upstream tracking silently.

    renovate.json pins each managed spec by an anchored path
    (`^src/gnome-51/gtk4/gtk4\\.spec$`), so a track that moves without it
    watches nothing and reports nothing — no error, no PR, just a stack
    quietly falling behind upstream.

    This is not a hypothetical failure mode; it is a recorded one, on this
    file. Its own description says src/gnome-51 forked from src/gnome-50
    without carrying its Renovate coverage, "so gtk4/libadwaita/gnome-shell
    etc. drifted silently until #580 caught it by hand". The guard above
    caught its re-introduction automatically, four hours after it landed on
    main.
    """
    assert "renovate.json" in br.TRACK_DECLARATIONS
    text = (REPO / "renovate.json").read_text(encoding="utf-8")
    tracks = live_tracks()
    watched = {track for track in tracks if track in text}
    assert watched == tracks, (
        "these live release tracks have no Renovate manager, so nothing "
        f"tells us when upstream moves: {sorted(tracks - watched)}"
    )
