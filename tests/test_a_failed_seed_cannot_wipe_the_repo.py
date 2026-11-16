"""A failed R2 seed must stop the job, not wipe the published repo (#627).

build.yml and build-distributed.yml seed local-repo from R2 and later
`rclone sync` it up to repo/10-stream-x86_64 and repo/10-x86_64. `rclone sync`
makes the destination MATCH the source, so a seed that failed but was
swallowed with `|| true` leaves a tree that, synced up, deletes every package
that did not come down. That is the INCIDENT-repo-wipe-gnome.md mechanism.

Two gates close it: the seed step fails when rclone fails, and a step before
the upload refuses a tree with no RPMs or no repodata. These tests run the
real step scripts under bash with stub rclone/createrepo_c binaries.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
GENERATOR = REPO / "scripts" / "generate-distributed-workflow.py"


def _generated_workflow(tmp_path):
    spec = importlib.util.spec_from_file_location("gen_dw_seed", GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    manifest = tmp_path / "order.yml"
    manifest.write_text(yaml.safe_dump(
        {"tiers": [{"name": "core", "packages": [{"path": "src/core/glibc"}]}]}))
    out = tmp_path / "wf.yml"
    mod.generate_workflow(str(manifest), str(out))
    return yaml.safe_load(out.read_text())


def _workflow(name, tmp_path):
    if name == "generated":
        return _generated_workflow(tmp_path)
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _step(workflow, job, name):
    for step in workflow["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"{job} has no step named {name!r}")


def _run(script, tmp_path, rclone_rc=0):
    """Run a step script as GitHub does (bash -e) with stubbed tools."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "rclone").write_text(f"#!/bin/sh\nexit {rclone_rc}\n")
    (bindir / "createrepo_c").write_text(
        '#!/bin/sh\nfor d; do :; done\nmkdir -p "$d/repodata" && : > "$d/repodata/repomd.xml"\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "R2_BUCKET": "bucket",
        "LOCAL_REPO": str(tmp_path / "local-repo"),
    }
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                          cwd=tmp_path, env=env, capture_output=True, text=True)


SEEDS = [
    ("build.yml", "build"),
    ("build-distributed.yml", "seed-repo"),
    ("generated", "seed-repo"),
]

GUARDS = [
    ("build.yml", "build", "Upload to R2"),
    ("build-distributed.yml", "publish", "Upload to R2"),
    ("generated", "publish", "Upload to R2"),
]


@pytest.mark.parametrize("name,job", SEEDS)
def test_a_failed_seed_fails_the_step(name, job, tmp_path):
    script = _step(_workflow(name, tmp_path), job, "Seed local repo from R2")["run"]
    result = _run(script, tmp_path, rclone_rc=1)
    assert result.returncode != 0, (
        f"{name}: the seed step succeeded although rclone failed; the later "
        "`rclone sync` would then delete what did not come down.")


@pytest.mark.parametrize("name,job", SEEDS)
def test_a_good_seed_passes(name, job, tmp_path):
    script = _step(_workflow(name, tmp_path), job, "Seed local repo from R2")["run"]
    result = _run(script, tmp_path, rclone_rc=0)
    assert result.returncode == 0, result.stderr


def test_the_generator_seeds_an_unpublished_prefix(tmp_path):
    """rclone exit 3 is "directory not found": nothing published, nothing to delete."""
    script = _step(_generated_workflow(tmp_path), "seed-repo", "Seed local repo from R2")["run"]
    assert _run(script, tmp_path, rclone_rc=3).returncode == 0


def _guard(name, job, upload, tmp_path):
    steps = _workflow(name, tmp_path)["jobs"][job]["steps"]
    names = [s.get("name") for s in steps]
    assert "Refuse to publish an empty result" in names, f"{name}: no empty-repo guard"
    assert names.index("Refuse to publish an empty result") < names.index(upload), (
        f"{name}: the guard must run before the upload syncs up")
    return steps[names.index("Refuse to publish an empty result")]["run"]


@pytest.mark.parametrize("name,job,upload", GUARDS)
def test_the_guard_refuses_an_empty_tree(name, job, upload, tmp_path):
    script = _guard(name, job, upload, tmp_path)
    (tmp_path / "local-repo" / "repodata").mkdir(parents=True)
    (tmp_path / "local-repo" / "repodata" / "repomd.xml").write_text("<repomd/>")
    assert _run(script, tmp_path).returncode != 0


@pytest.mark.parametrize("name,job,upload", GUARDS)
def test_the_guard_refuses_a_tree_without_repodata(name, job, upload, tmp_path):
    script = _guard(name, job, upload, tmp_path)
    (tmp_path / "local-repo").mkdir()
    (tmp_path / "local-repo" / "a-1-1.x86_64.rpm").write_text("")
    assert _run(script, tmp_path).returncode != 0


@pytest.mark.parametrize("name,job,upload", GUARDS)
def test_the_guard_passes_a_real_tree(name, job, upload, tmp_path):
    script = _guard(name, job, upload, tmp_path)
    (tmp_path / "local-repo" / "repodata").mkdir(parents=True)
    (tmp_path / "local-repo" / "repodata" / "repomd.xml").write_text("<repomd/>")
    (tmp_path / "local-repo" / "a-1-1.x86_64.rpm").write_text("")
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr
