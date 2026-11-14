"""Ubuntu's current interim release is its own target, ubuntu-rolling.

TunaOS stingray follows `ubuntu:rolling` (today 26.10 "Stonking Stingray"),
and the `ubuntu` target builds for resolute only, so its debs cannot qualify
stingray. These tests keep the two targets apart, keep the interim one pinned
to a release rather than the moving tag, and pin the publish label that the
served repo tideforge/ubuntu-rolling depends on.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tideforge  # noqa: E402

FACTORY = yaml.safe_load((ROOT / "manifests" / "package-factory.yaml").read_text(encoding="utf-8"))
TUNA = ROOT / "packages" / "tuna-desktop" / "package.yaml"


def test_the_interim_target_builds_the_interim_release_not_resolute():
    rolling = FACTORY["targets"]["ubuntu-rolling"]
    assert rolling["format"] == "deb"
    assert rolling["suites"] == ["stonking"]
    assert rolling["probe_image"] == "docker.io/library/ubuntu:26.10"
    assert rolling["architectures"] == ["amd64", "arm64"]
    assert FACTORY["targets"]["ubuntu"]["suites"] == ["resolute"]
    assert FACTORY["targets"]["ubuntu"]["probe_image"] == "docker.io/library/ubuntu:26.04"


def test_the_build_image_is_a_release_not_the_moving_rolling_tag():
    """`ubuntu:rolling` jumps to the next interim release on its release day.
    Building from it would change these bytes while `suites` still named the
    old release; the move must be one reviewed edit of both."""
    assert not FACTORY["targets"]["ubuntu-rolling"]["probe_image"].endswith(":rolling")


def test_no_index_is_asserted_before_the_first_publish():
    rolling = FACTORY["targets"]["ubuntu-rolling"]
    assert "published_index" not in rolling
    assert rolling["published_index_pending"] == ["amd64", "arm64"]


def test_stingray_consumes_the_interim_target():
    adapter = FACTORY["consumer_adapters"]["stingray"]
    assert adapter["target"] == "ubuntu-rolling"
    assert adapter["manager"] == "apt"
    assert FACTORY["consumer_adapters"]["grouper"]["target"] == "ubuntu"


def test_tuna_desktop_renders_for_the_interim_release_with_the_system_layer_shell():
    recipe = tideforge.load_yaml(TUNA)
    tideforge.validate(recipe, "ubuntu-rolling")
    rendered = tideforge.render(recipe, "ubuntu-rolling")
    control = rendered["debian/control"]
    assert "libgtk4-layer-shell-dev (>= 1.1)" in control
    assert "rustc (>= 1.92)" in control
    assert "libgtk4-layer-shell0 (>= 1.1)" in control
    # trixie's private gtk4-layer-shell never reaches this target.
    assert "patchelf" not in control
    assert "gtk4-layer-shell-stage" not in rendered["debian/tideforge-build.sh"]


def test_ubuntu_rolling_publishes_to_its_own_repo_and_verifies_tuna_desktop():
    plan = json.loads(subprocess.run(
        [sys.executable, "scripts/plan-deb-publish.py", "--packages", "tuna-desktop", "--distros", "ubuntu-rolling"],
        capture_output=True, text=True, cwd=ROOT, check=True,
    ).stdout)
    rows = plan["matrix"]["include"]
    assert plan["distros"] == ["ubuntu-rolling"]
    assert {row["target"] for row in rows} == {"ubuntu-rolling"}
    assert {row["arch"] for row in rows} == {"amd64", "arm64"}
    assert {row["image"] for row in rows} == {"docker.io/library/ubuntu:26.10"}
    assert {row["install"] for row in plan["verify_matrix"]["include"]} == {"tuna-desktop"}


def test_the_receipt_binds_the_ubuntu_rolling_label_to_its_target():
    spec = importlib.util.spec_from_file_location(
        "deb_verification_receipt", ROOT / "scripts" / "deb-verification-receipt.py"
    )
    receipt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(receipt)
    assert receipt.TARGETS["ubuntu-rolling"] == "ubuntu-rolling"
    assert receipt.TARGETS["ubuntu"] == "ubuntu"
