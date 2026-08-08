"""Throughput invariants for `Build Hummingbird desktops` (issue #267).

Both things pinned here are invisible when broken.  A dropped `MOCK_CACHE_DIR`
does not fail a build, it just makes every package rebuild the same minimal
buildroot again -- which is how 34% of the measured mock time was being spent
without anyone noticing (docs/hummingbird-throughput.md, Finding 2).  A matrix
collapsed back to one job does not fail either, it just goes five times slower
and, for `desktop: all`, hits the 360-minute job cap it always used to.

Numbers behind these: five real runs, 194 distinct packages, 6.80 h of mock
time; the build step is 95.6%-97.8% mock in every one of them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from plan_hummingbird_matrix import desktops_in, main, plan  # noqa: E402

WORKFLOW = ROOT / ".github/workflows/build-hummingbird-desktops.yml"
MANIFEST = ROOT / "build-order-hummingbird-desktops.yml"


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text())


def step(name: str) -> dict:
    return next(
        s for s in workflow()["jobs"]["build"]["steps"] if s.get("name") == name
    )


# --- the shared buildroot ---------------------------------------------------


def test_build_step_gives_mock_a_cache_directory() -> None:
    """Without this, /var/cache/mock dies with each package's container."""
    env = step("Build tiers").get("env") or {}
    assert "MOCK_CACHE_DIR" in env, (
        "the Build tiers step sets no MOCK_CACHE_DIR, so build-chain.sh mounts "
        "nothing at /var/cache/mock and every package re-runs `installing "
        "minimal buildroot with dnf5` from scratch -- 43s x 194 packages = 34% "
        "of the measured mock time"
    )


def test_the_cache_is_shared_by_every_package_in_the_job() -> None:
    """One path for the whole job, not one per package or per tier.

    The value is the entire point: mock keys its root cache on the config's
    root name from BEFORE --uniqueext is appended (buildroot.py
    `shared_root_name`), so a single directory is shared by all of them.  A
    path interpolating the tier or the package would hand each build its own
    empty cache and change nothing.
    """
    value = (step("Build tiers")["env"])["MOCK_CACHE_DIR"]
    for varying in ("matrix.", "steps.", "tier", "package"):
        assert varying not in value, (
            f"MOCK_CACHE_DIR is {value!r}, which varies per {varying!r}; the "
            "cache has to be one directory for the whole job or nothing can "
            "ever hit it"
        )
    assert "runner.temp" in value, (
        f"MOCK_CACHE_DIR is {value!r}; it must be a real runner path for "
        "build-chain.sh to bind-mount into the build container, and it must "
        "sit outside the checkout -- keepcache=1 means it fills with unsigned "
        "third-party RPMs that no workspace glob should ever be able to reach"
    )


# --- one job per desktop ----------------------------------------------------


def test_build_is_a_matrix_over_desktops() -> None:
    build = workflow()["jobs"]["build"]
    matrix = build.get("strategy", {}).get("matrix", {})
    assert "desktop" in matrix, (
        "the build job is not matrixed over desktops, so the whole 680-package "
        "gap is driven one dispatch at a time on one of the org's 60 runners"
    )
    assert "fromJson" in matrix["desktop"], (
        "the desktop axis must come from the plan job's JSON output, not a "
        "list hand-kept in step with the manifest"
    )


def test_the_matrix_comes_from_the_plan_job() -> None:
    build = workflow()["jobs"]["build"]
    needs = build["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "plan" in needs
    assert "desktops" in workflow()["jobs"]["plan"]["outputs"]


def test_one_desktop_failing_does_not_cancel_the_others() -> None:
    """They share nothing but R2, and R2 publishing is copy-not-sync."""
    assert workflow()["jobs"]["build"]["strategy"]["fail-fast"] is False


def test_the_tier_filter_follows_the_matrix_not_the_input() -> None:
    """`inputs.desktop` is the fan-out; `matrix.desktop` is this job's share.

    The selection itself is scripts/select-desktop-tiers.py (#276), which
    resolves a desktop through the gap report rather than by tier-name prefix;
    what this pins is which desktop that script is asked about.
    """
    select = step("Select tiers")
    assert select["env"]["IN_DESKTOP"] == "${{ matrix.desktop }}", (
        "Select tiers still reads inputs.desktop, so every matrix job would "
        "select the same tiers and build the identical work five times"
    )
    assert "inputs.desktop" not in select["run"], (
        "the run block reaches around IN_DESKTOP and back to the dispatch "
        "input, which collapses the matrix"
    )


def test_every_artifact_name_is_unique_per_matrix_job() -> None:
    """Two jobs uploading one name is a hard failure in upload-artifact v4+."""
    uploads = [
        s
        for s in workflow()["jobs"]["build"]["steps"]
        if str(s.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert uploads, "the build job uploads nothing at all"
    for up in uploads:
        name = up["with"]["name"]
        assert "matrix.desktop" in name, (
            f"artifact {name!r} is not per-desktop; with five concurrent jobs "
            "the second upload of that name fails the job"
        )


# --- the planner ------------------------------------------------------------


def test_desktops_come_out_of_the_manifest() -> None:
    assert desktops_in(manifest()) == ["gnome", "kde", "cosmic", "niri", "xfce"]


def test_bootstrap_is_not_a_desktop() -> None:
    """bootstrap-* tiers are #268's PEP-517 backends, prepended to every job."""
    assert not any(d.startswith("bootstrap") for d in desktops_in(manifest()))


def test_all_fans_out_to_every_desktop() -> None:
    assert plan(manifest(), "all", "") == desktops_in(manifest())


def test_a_single_desktop_is_a_single_job() -> None:
    assert plan(manifest(), "niri", "") == ["niri"]


def test_an_explicit_tier_list_stays_one_job() -> None:
    """A tier list names tiers absolutely, across whatever desktops own them.

    Splitting it per desktop would change which tiers run, not just where, and
    `tiers:` exists to resume a run exactly.
    """
    assert plan(manifest(), "gnome", "gnome-00,kde-00") == ["gnome"]
    assert plan(manifest(), "all", "niri-00") == ["all"]


def test_an_unknown_desktop_fails_the_plan_not_the_build() -> None:
    with pytest.raises(SystemExit):
        plan(manifest(), "budgie", "")


def test_the_plan_output_is_a_github_output_line() -> None:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["--manifest", str(MANIFEST), "--desktop", "all"])
    key, _, value = buf.getvalue().strip().partition("=")
    assert key == "desktops"
    assert json.loads(value) == desktops_in(manifest())


# --- the flag that made the cache worthless -------------------------------


def test_the_repo_mock_configs_are_copied_with_their_mtimes() -> None:
    """`cp -p`, not `cp`. Measured, not theorised.

    mock invalidates its root cache when any file in `config_paths` is newer
    than the cache tarball (`plugins/root_cache.py _unpack_root_cache`).
    build-chain.sh assembles /tmp/mock-configdir fresh inside every package's
    container, so a plain `cp` stamps the profile with the current time and
    the config is ALWAYS newer than a cache an earlier package wrote.

    Run 31268488082 had MOCK_CACHE_DIR set, the bind mount working and the
    tarball being written to /var/cache/mock/hummingbird-ci/root_cache/ --
    and still logged `hummingbird-ci.cfg newer than root cache; cache will be
    rebuilt` 18 times, unpacked the cache 0 times, and finished in 39.5m
    against a 39.0m no-cache baseline (31265993115).  The whole win rests on
    this one flag, and nothing else in the run reports its absence.
    """
    script = (ROOT / "scripts" / "build-chain.sh").read_text()
    assert "cp -p /repo-mock/*.cfg" in script, (
        "the repo mock profiles are copied into the container's configdir "
        "without -p, so every container re-stamps them with the current time "
        "and mock deletes the shared root cache before it can ever be read"
    )
