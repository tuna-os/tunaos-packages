"""A warm host is only useful if forgetting is exact in both directions.

scripts/warm-builder.sh keeps the local repo, the mock root cache and the
served-NVR list on a persistent volume, so build-chain.sh's existing skip
("this NVR is already in the local repo") turns a re-run into a rebuild of
only what changed. `--forget <package>` is the other half: it drops what a
package produced so the next run rebuilds it.

Both ways of getting that wrong are silent:

  TOO LITTLE  build-chain.sh's skip matches the MAIN binary's NVR only
              (`ls <name>-<v>-<r>*.rpm`). Removing just that leaves
              gtk4-devel-4.23.1 in the repo, so the rebuilt gtk4 lands beside
              headers from the build that failed and every dependent compiles
              against a -devel its own gtk4 never produced.
  TOO MUCH    gtk4-layer-shell is a different source package that shares a
              prefix. Forgetting it turns a one-package retry into a chain,
              which is the cost the warm host exists to avoid.

The cell's shape (manifest, mock config, image, served index) is asserted to
come from manifests/package-builds.yaml rather than from flags, because a
warm builder with its own idea of what a cell is would be a second definition
that drifts from the one CI uses -- and the failure mode of that drift is a
green local build of something CI cannot reproduce.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "warm-builder.sh"


def run(args: list[str], state: pathlib.Path,
        path_prefix: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, TUNAOS_WARM_STATE=str(state))
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO, env=env, capture_output=True, text=True,
    )


@pytest.fixture()
def stubbed(tmp_path: pathlib.Path):
    """A state dir with a populated local repo and a stub createrepo_c.

    `rpm` is deliberately NOT stubbed: the fallback path is what runs on a
    host without it, and it is the half a test can get wrong by never
    exercising.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "createrepo_c").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "createrepo_c").chmod(0o755)
    repo = tmp_path / "state" / "hummingbird-x86_64" / "local-repo"
    repo.mkdir(parents=True)
    for name in (
        "gtk4-4.23.1-1.fc43.x86_64.rpm",
        "gtk4-devel-4.23.1-1.fc43.x86_64.rpm",
        "gtk4-devel-docs-4.23.1-1.fc43.noarch.rpm",
        "gtk4-layer-shell-1.2.0-1.fc43.x86_64.rpm",
        "mutter-51.0-1.fc43.x86_64.rpm",
    ):
        (repo / name).touch()
    return tmp_path / "state", bindir, repo


def test_forget_drops_every_subpackage_of_the_source(stubbed) -> None:
    state, bindir, repo = stubbed
    result = run(
        ["--cell", "hummingbird-x86_64", "--forget", "gtk4", "--status"],
        state, bindir,
    )
    assert result.returncode == 0, result.stderr
    survivors = sorted(p.name for p in repo.glob("*.rpm"))
    assert survivors == [
        "gtk4-layer-shell-1.2.0-1.fc43.x86_64.rpm",
        "mutter-51.0-1.fc43.x86_64.rpm",
    ], survivors


def test_forget_does_not_take_a_package_that_merely_shares_a_prefix(
    stubbed,
) -> None:
    state, bindir, repo = stubbed
    run(["--cell", "hummingbird-x86_64", "--forget", "gtk4", "--status"],
        state, bindir)
    assert (repo / "gtk4-layer-shell-1.2.0-1.fc43.x86_64.rpm").exists(), (
        "gtk4-layer-shell is a different source package at a different "
        "version-release; forgetting it turns a one-package retry into a chain"
    )


def test_forgetting_something_that_was_never_built_is_not_an_error(
    stubbed,
) -> None:
    state, bindir, _ = stubbed
    result = run(
        ["--cell", "hummingbird-x86_64", "--forget", "not-here", "--status"],
        state, bindir,
    )
    assert result.returncode == 0, result.stderr
    assert "nothing banked for not-here" in result.stderr


def test_the_cell_shape_comes_from_the_manifest(tmp_path: pathlib.Path) -> None:
    builds = yaml.safe_load(
        (REPO / "manifests" / "package-builds.yaml").read_text()
    )
    for cell in builds["native_builds"]:
        if cell.get("enabled") is False:
            continue
        result = run(["--cell", cell["id"], "--status"], tmp_path)
        assert result.returncode == 0, result.stderr
        assert cell["manifest"] in result.stdout, cell["id"]
        assert cell["mock_config"] in result.stdout, cell["id"]


def test_an_unknown_cell_names_the_ones_that_exist(
    tmp_path: pathlib.Path,
) -> None:
    result = run(["--cell", "not-a-cell", "--status"], tmp_path)
    assert result.returncode == 2
    assert "hummingbird-x86_64" in result.stderr


def test_status_on_a_cold_host_is_not_an_error(tmp_path: pathlib.Path) -> None:
    """The first thing anyone runs is --status, before any state exists."""
    result = run(["--cell", "hummingbird-x86_64", "--status"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "banked RPMs 0" in result.stdout


def test_the_root_cache_is_pointed_at_the_persistent_state() -> None:
    """MOCK_CACHE_DIR under the state dir is what makes run 2 cheap.

    package-factory-cell.yml deliberately points it at runner.temp, where the
    win is only WITHIN one job (docs/hummingbird-throughput.md Finding 2:
    rebuilding the same minimal buildroot is 34.1% of all mock time). A warm
    host that inherited that choice would rebuild the root cache every run and
    keep the expensive half of the cold start.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'export MOCK_CACHE_DIR="${STATE}/mock-cache"' in text


def test_the_warm_builder_never_publishes() -> None:
    """Promotion stays with the gated publishers (INCIDENT-repo-wipe-gnome)."""
    text = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("rclone", "rpmsign", "R2_BUCKET", "publish-rpm-wave"):
        assert forbidden not in text, (
            f"the warm builder references {forbidden}; its local repo is a "
            "bringup workspace, not a repository anyone consumes"
        )
