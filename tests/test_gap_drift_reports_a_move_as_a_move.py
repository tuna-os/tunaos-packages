"""A regenerated build order must not describe a version downgrade as churn.

The drift job opens a PR whose body is the only thing most reviewers judge it
by. The old summary compared FULL PATHS, so a package that moved between spec
directories could never cancel between the drop and add lists -- it appeared
in both, under headings that were each false for it:

    Dropped from the build set (the target now ships these):
      src/gnome-51/gdm
    Added to the build set (the target stopped shipping, ...):
      src/gnome-50/gdm

tuna-os/tunaos-packages#542 (2026-08-26) is the measured case: all 19 GNOME
packages moved from src/gnome-51 to src/gnome-50, a desktop-wide downgrade of
gdm, gnome-shell, mutter and gtk4, presented as the target having adopted
them. It ships none of them -- that is the premise of the gap measurement.

So a move is its own category, reported first and showing both paths.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "summarize-gap-drift.py"
WORKFLOW = ROOT / ".github" / "workflows" / "upstream-drift.yml"


def summarize(diff: str, target: str = "hummingbird") -> str:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--target", target],
        input=diff, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def diff_for(removed, added) -> str:
    lines = ["--- a/build-order.yml", "+++ b/build-order.yml", "@@ -1 +1 @@"]
    lines += [f"-      - path: {p}" for p in removed]
    lines += [f"+      - path: {p}" for p in added]
    return "\n".join(lines) + "\n"


def test_a_version_track_change_is_called_a_move():
    body = summarize(diff_for(["src/gnome-51/gdm"], ["src/gnome-50/gdm"]))
    assert "MOVED" in body, body
    assert "gdm: src/gnome-51/gdm -> src/gnome-50/gdm" in body, body
    # and NOT under either of the two headings that would be false
    assert "the target now ships these" not in body, body
    assert "the target stopped shipping" not in body, body


def test_the_whole_gnome_downgrade_is_reported_as_moves():
    """The #542 shape: 19 packages, one track to another."""
    names = ["gdm", "gnome-shell", "mutter", "gtk4", "libadwaita", "gjs",
             "nautilus", "vte291", "ptyxis", "gnome-session"]
    body = summarize(diff_for([f"src/gnome-51/{n}" for n in names],
                              [f"src/gnome-50/{n}" for n in names]))
    assert f"**{len(names)} package(s) MOVED" in body, body
    for name in names:
        assert f"{name}: src/gnome-51/{name} -> src/gnome-50/{name}" in body, name


def test_a_real_drop_is_still_a_drop():
    body = summarize(diff_for(["src/hummingbird/jsoncpp"], []))
    assert "the target now ships these" in body, body
    assert "src/hummingbird/jsoncpp" in body, body
    assert "MOVED" not in body, body


def test_a_real_add_is_still_an_add():
    body = summarize(diff_for([], ["src/hummingbird/mako"]))
    assert "the target stopped shipping" in body, body
    assert "src/hummingbird/mako" in body, body
    assert "MOVED" not in body, body


def test_a_line_rewritten_in_place_is_not_a_move():
    """-U0 shows a reordered line as both a removal and an addition. Calling
    that `lame: src/hummingbird/lame -> src/hummingbird/lame` a move teaches
    readers to skim the one list that must be read."""
    body = summarize(diff_for(["src/hummingbird/lame"], ["src/hummingbird/lame"]))
    assert "MOVED" not in body, body
    assert "No package set changes" in body, body


def test_no_changes_says_so():
    assert "No package set changes" in summarize(diff_for([], []))


def test_moves_drops_and_adds_can_coexist():
    body = summarize(diff_for(
        ["src/gnome-51/gdm", "src/hummingbird/jsoncpp"],
        ["src/gnome-50/gdm", "src/hummingbird/mako"]))
    assert "MOVED" in body
    assert "gdm: src/gnome-51/gdm -> src/gnome-50/gdm" in body
    assert "src/hummingbird/jsoncpp" in body
    assert "src/hummingbird/mako" in body


def test_the_workflow_actually_calls_this_script():
    """The script can be perfect and unreferenced; the old inline bash would
    still be what opens the PR."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    bodies = [s.get("run", "") for job in spec["jobs"].values()
              for s in job.get("steps", [])]
    joined = "\n".join(bodies)
    assert "scripts/summarize-gap-drift.py" in joined, joined[:400]
    # the inline version's tell-tale, which must be gone
    assert "really_dropped" not in joined, "the old inline summary is still there"
