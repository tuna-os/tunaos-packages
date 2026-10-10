"""Both publishers must share the wave logic, and must not race each other.

Two independent failure modes, both already paid for once in this repo:

  DRIFT     the nightly cron stagger was documented and never applied; the
            readiness stamp was read from two paths flatpak had stopped
            using. A hand-copied second publisher rots the same way, except
            the thing that rots holds credentials that can empty a live repo.

  RACE      both publishers `rclone sync` into the same bucket, and sync
            makes the destination match the source. Two at once delete each
            other's packages (#124 / INCIDENT-repo-wipe-gnome). A shared
            concurrency group is the only thing serialising them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
TIDEFORGE = WORKFLOWS / "publish-tideforge-rpms.yml"
BUILD_CHAIN = WORKFLOWS / "publish-build-chain-rpms.yml"
WAVE = ROOT / "scripts" / "publish-rpm-wave.sh"

PUBLISHERS = (TIDEFORGE, BUILD_CHAIN)


def doc(path):
    return yaml.safe_load(path.read_text())


@pytest.mark.parametrize("path", PUBLISHERS, ids=lambda p: p.name)
def test_the_publisher_calls_the_shared_script(path) -> None:
    body = path.read_text()
    assert "scripts/publish-rpm-wave.sh" in body, (
        f"{path.name} does not use the shared wave script; a second copy of "
        "the sign/index rules will drift from the first"
    )


@pytest.mark.parametrize("path", PUBLISHERS, ids=lambda p: p.name)
def test_the_publisher_does_not_reimplement_the_rules(path) -> None:
    """The specific incantations that must live in exactly one place."""
    # Comments may name these; the point is that no publisher RUNS them.
    run_blocks = "\n".join(
        step.get("run", "")
        for job in doc(path)["jobs"].values()
        for step in (job.get("steps") or [])
    )
    for token in ("rpmsign --addsign", "createrepo_c"):
        assert token not in run_blocks, (
            f"{path.name} runs `{token}` directly instead of via the shared "
            "script"
        )


def test_both_publishers_share_one_concurrency_group() -> None:
    """The race guard. Distinct groups let them run together and sync over
    each other."""
    groups = {p.name: doc(p)["concurrency"]["group"] for p in PUBLISHERS}
    assert len(set(groups.values())) == 1, (
        f"publishers are in different concurrency groups: {groups}"
    )


@pytest.mark.parametrize("path", PUBLISHERS, ids=lambda p: p.name)
def test_the_publisher_does_not_cancel_in_progress(path) -> None:
    """Cancelling mid-sync leaves the bucket half-written."""
    assert doc(path)["concurrency"]["cancel-in-progress"] is False


def test_the_wave_script_is_executable_and_self_documenting() -> None:
    assert WAVE.exists()
    text = WAVE.read_text()
    for rule in ("EMPTY WAVE", "NEVER SHRINK", "'+' IN NAMES", "SRPMS EXCLUDED"):
        assert rule in text, f"the {rule} rule lost its reasoning"


# --- the #179 job -----------------------------------------------------------


@pytest.mark.parametrize("path", PUBLISHERS, ids=lambda p: p.name)
def test_the_publisher_verifies_what_it_served(path) -> None:
    """Neither publisher may trust its own exit code.

    "the workflow exited 0" and "dnf can install it" are different claims and
    only the second matters. #463 shipped without this; the check found a
    real wrong assumption on its first use (#466).
    """
    jobs = doc(path)["jobs"]
    assert "verify" in jobs, (
        f"{path.name} has no verify job -- a repo that was never actually "
        "published would sit behind a green workflow (#179)"
    )
    assert "publish" in jobs["verify"]["needs"]


def test_the_build_chain_verify_does_not_run_on_a_dry_run() -> None:
    """A dry run syncs nothing, so verifying the served index would fail on
    the previous wave's contents and read as a publish defect."""
    verify_job = doc(BUILD_CHAIN)["jobs"]["verify"]
    assert "dry_run" in str(verify_job.get("if", ""))
