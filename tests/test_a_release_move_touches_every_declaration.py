"""Guard release-track classification and atomic adoption.

Files naming live tracks must be classified so adoption cannot silently miss
an important declaration. Categories must be exclusive, and adoption must
rewrite declarations together while leaving decision files alone.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import build_request as br  # noqa: E402

# Generated from the measurement and rewritten wholesale by the gap engine, so
# a hand rewrite here would be overwritten by the next `mode: propose` PR --
# and would race it. Excluded by KIND, not by name: any build order is
# regenerated.
GENERATED = re.compile(r"^build-order.*\.ya?ml$|^\.copr/build-order")


def tracked_files() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split("\n")
    keep = []
    for name in out:
        if not name:
            continue
        # src/ is the tree being moved, tests/ names tracks to test the move,
        # docs/ is prose. None is a declaration `--adopt` reads.
        if name.startswith(("src/", "tests/", "docs/", "_upstream-snapshots/")):
            continue
        if GENERATED.match(name):
            continue
        keep.append(pathlib.Path(name))
    return keep


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


def test_every_file_that_names_a_track_is_classified() -> None:
    classified = set(
        br.TRACK_DECLARATIONS + br.TRACK_DECISIONS + br.TRACK_HISTORY
    )
    tracks = live_tracks()
    unclassified = []
    for relative in tracked_files():
        path = REPO / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(track in text for track in tracks) and str(relative) not in classified:
            unclassified.append(str(relative))
    assert not unclassified, (
        "these files name a release track and are in none of "
        "TRACK_DECLARATIONS / TRACK_DECISIONS / TRACK_HISTORY, so a "
        f"`--adopt` would move the tree around them: {unclassified}"
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
