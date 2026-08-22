"""Arch packages must be able to reach a consumer.

Arch was the last format with no publisher at all (#479). 23 recipes target it
-- the DankMaterialShell stack, niri, greetd, bazaar, libseat -- and every one
was planned, built, verified, smoke-tested and then unreachable, with
docs/FACTORY-STATUS.md reporting "no published_index declared".

The two hazards this publisher must not repeat, both learned the hard way in
scripts/arch-clean-install.sh:

  db naming     repo-add writes <name>.db.tar.gz, but pacman requests
                <name>.db. On a filesystem that is a symlink repo-add makes;
                over HTTP from an object store there are no symlinks, so the
                db must exist under the requested name as a real object.

  repo ordering pacman resolves `-S <name>` by walking sync repositories in
                CONFIGURATION ORDER and taking the first that provides the
                name -- it does not compare versions. With our repository
                listed after [core]/[extra], every package that also exists in
                official Arch would be installed FROM Arch. Run 31113235209
                built bazaar 0.9.1-1 and reported 0.9.2-1 from `pacman -Q`;
                niri, greetd and dgop all exist upstream too.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLANNER = ROOT / "scripts" / "plan-arch-publish.py"
WORKFLOW = ROOT / ".github" / "workflows" / "publish-tideforge-arch.yml"
WAVE = ROOT / "scripts" / "publish-arch-wave.sh"
VERIFY = ROOT / "scripts" / "arch-verify-published.sh"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PLANNER), *args], cwd=ROOT, capture_output=True, text=True)


def planned(*args: str) -> dict:
    result = run(*args)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_publisher_exists():
    assert WORKFLOW.is_file()


def test_it_is_dispatch_only():
    """Publishing is a deliberate act, not a side effect of a merge — the same
    posture as the rpm and deb publishers."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = spec[True] if True in spec else spec["on"]
    assert list(triggers) == ["workflow_dispatch"]


def test_it_shares_the_publish_concurrency_group():
    """All publishers rclone sync into the same bucket, and sync makes the
    destination match the source, so two at once can delete each other's
    packages (#124)."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert spec["concurrency"]["group"] == "publish-rpms"
    assert spec["concurrency"]["cancel-in-progress"] is False


def test_it_publishes_to_the_contract_r2_path():
    factory = yaml.safe_load((ROOT / "manifests" / "package-factory.yaml").read_text(encoding="utf-8"))
    r2 = factory["targets"]["arch"]["r2_path"]
    for row in planned("--packages", "greetd")["publish"]["include"]:
        assert row["src"] == r2.replace("{arch}", row["arch"])


def test_the_db_is_written_under_the_name_pacman_requests():
    """repo-add produces <name>.db.tar.gz; pacman asks for <name>.db. Over
    HTTP there is no symlink to bridge them."""
    text = WAVE.read_text(encoding="utf-8")
    assert 'cp -f "$repo/$name.db.tar.gz" "$repo/$name.db"' in text


def test_our_repository_is_configured_before_core_and_extra():
    """The ordering hazard. If [core] appeared first, a package that also
    exists upstream would be installed from Arch and the verify would assert
    nothing about what we published."""
    text = VERIFY.read_text(encoding="utf-8")
    ours = text.index('echo "[${REPO_NAME}]"')
    assert ours < text.index("echo '[core]'")
    assert ours < text.index("echo '[extra]'")


def test_the_verify_asserts_each_package_came_from_our_repository():
    """Ordering alone is a precaution; this is the assertion that would have
    caught run 31113235209."""
    text = VERIFY.read_text(encoding="utf-8")
    assert "resolves to repository" in text
    assert 'if [ "$origin" != "$REPO_NAME" ]' in text


def test_the_wave_refuses_to_publish_nothing():
    """An empty wave would rclone sync an empty tree over the live repository."""
    assert "refusing to publish an empty wave" in WAVE.read_text(encoding="utf-8")


def test_the_wave_refuses_to_let_the_repository_shrink():
    assert "refusing to sync" in WAVE.read_text(encoding="utf-8")


def test_a_package_that_does_not_target_arch_is_refused():
    result = run("--packages", "evtest")
    assert result.returncode != 0
    assert "does not target arch" in result.stderr


def test_the_sync_down_excludes_the_db_it_regenerates():
    """The db is rebuilt from the full package set; round-tripping a stale one
    risks publishing an index that disagrees with the payloads beside it."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '--exclude "*.db*"' in text


def test_the_curated_default_wave_is_an_explicit_list():
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = spec[True] if True in spec else spec["on"]
    default = triggers["workflow_dispatch"]["inputs"]["packages"]["default"]
    assert default and "," in default
    for name in default.split(","):
        assert (ROOT / "packages" / name.strip() / "package.yaml").is_file(), name


def steps_invoking(script: str) -> list[dict]:
    """Every step in the workflow whose run block mentions `script`."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return [
        step
        for job in spec["jobs"].values()
        for step in job.get("steps", [])
        if script in (step.get("run") or "")
    ]


def test_indexing_happens_inside_the_arch_container():
    """repo-add ships in Arch's `pacman` package and does NOT exist on
    ubuntu-latest. A host-side invocation fails on the first dispatch — which
    is exactly what the first draft of this workflow did, installing only
    libarchive-tools/zstd/gpg and then calling repo-add.

    Asserted per-step rather than by scanning a window of the file: a window
    wide enough to hold the step also holds its neighbours, so a host-side
    invocation sitting next to any other `docker run` reads as containerised.
    """
    invocations = steps_invoking("publish-arch-wave.sh")
    assert invocations, "nothing runs the wave script"
    for step in invocations:
        run = step["run"]
        where = step.get("name", "<unnamed>")
        assert "docker run" in run, f"{where}: wave script must run in the Arch container"
        assert "needs.plan.outputs.image" in run, f"{where}: must use the planned Arch image"
        # The container path proves the mount, and a host-relative path would
        # not resolve inside the image.
        assert "/scripts/publish-arch-wave.sh" in run, f"{where}: must use the mounted path"
        assert "bash scripts/publish-arch-wave.sh" not in run, f"{where}: host-side invocation"
        assert run.index("docker run") < run.index("publish-arch-wave.sh"), (
            f"{where}: the script is invoked before any container is started"
        )


def test_no_host_step_reaches_for_pacman_tooling():
    """The counterpart guard: repo-add/repo-remove must not appear in a step
    that has not entered the container."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in spec["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run") or ""
            if "repo-add" in run:
                assert "docker run" in run, step.get("name", "<unnamed>")


def test_the_wave_script_refuses_to_run_without_repo_add():
    """Belt and braces: if someone moves the step back onto the host, the
    script says why rather than dying on 'repo-add: command not found'."""
    text = WAVE.read_text(encoding="utf-8")
    assert "command -v repo-add" in text
    assert "must run inside an Arch container" in text


def test_the_host_does_not_pretend_to_install_repo_add():
    """apt has no package providing repo-add; installing libarchive-tools and
    zstd looks like it addresses this and does not."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "apt-get install" not in text


def test_the_signing_key_is_imported_where_it_is_used():
    """Mounting the runner's ~/.gnupg into the container brings permission and
    uid-mapping problems; importing inside avoids both."""
    text = WAVE.read_text(encoding="utf-8")
    assert "GPG_PRIVATE_KEY" in text and "gpg --batch --import" in text
    assert "$HOME/.gnupg" not in WORKFLOW.read_text(encoding="utf-8")
