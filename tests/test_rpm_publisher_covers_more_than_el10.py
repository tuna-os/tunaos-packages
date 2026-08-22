"""The RPM publisher must be able to publish a target other than el10.

publish-tideforge-rpms.yml hard-coded el10 in six places: the plan job's
`"el10" not in targets` check, CELL_ID, TARGET, IMAGE, a `case $arch` for the
R2 sync path, and a second `case $arch` for the served verify URL. That is why
opensuse-tumbleweed has no publisher despite declaring
`r2_path: rpm/opensuse-tumbleweed/{arch}` in the contract (#479) — its
packages are gated, verified, smoke-tested and unreachable.

`status` does not gate publishing: hummingbird is `scaffold` and publishes to
a live index, so the absence cannot be read as intentional on that basis.

THE SAFETY PROPERTY: el10's paths are bespoke and must not change. It does not
publish to its own contract r2_path — x86_64 syncs into repo/10-stream-x86_64
and mirrors to repo/10-x86_64, the pre-existing COPR-mirror repo this factory
merges into, while rpm/el10/x86_64 404s. Deriving el10 from the contract would
silently redirect a live repository. The first test below pins the emitted
values against exactly what the workflow used to hard-code.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLANNER = ROOT / "scripts" / "plan-rpm-publish.py"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PLANNER), *args], cwd=ROOT, capture_output=True, text=True)


def planned(*args: str) -> dict:
    result = run(*args)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def publish_rows(d: dict) -> dict:
    return {row["arch"]: row for row in d["publish"]["include"]}


def test_el10_paths_are_byte_identical_to_what_was_hardcoded():
    """The values below are copied from the workflow as it was before this
    change. A diff here means a live repository is about to be redirected."""
    rows = publish_rows(planned("--target", "el10", "--packages", "libunwind-devel"))
    assert rows["x86_64"]["src"] == "repo/10-stream-x86_64"
    assert rows["x86_64"]["mirror"] == "repo/10-x86_64"
    assert rows["x86_64"]["served"] == "https://repo.tunaos.org/repo/10/x86_64/"
    assert rows["aarch64"]["src"] == "rpm/el10/aarch64"
    assert rows["aarch64"]["mirror"] == ""
    assert rows["aarch64"]["served"] == "https://repo.tunaos.org/rpm/el10/aarch64/"


def test_el10_x86_64_keeps_the_repo_wipe_guard():
    """#124: rclone sync makes the destination match the source, so a partial
    sync-down DELETES the served repo. The x86_64 prefix holds hundreds of
    packages and must refuse to proceed from a small download."""
    rows = publish_rows(planned("--target", "el10", "--packages", "libunwind-devel"))
    assert rows["x86_64"]["min_rpms"] == 100


def test_a_prefix_this_workflow_creates_has_no_such_guard():
    """el10 aarch64 and every new target start empty by design; a minimum
    would make the first publish impossible."""
    rows = publish_rows(planned("--target", "el10", "--packages", "libunwind-devel"))
    assert rows["aarch64"]["min_rpms"] == 0
    suse = publish_rows(planned("--target", "opensuse-tumbleweed", "--packages", "quickshell"))
    assert all(row["min_rpms"] == 0 for row in suse.values())


def test_opensuse_can_be_planned_at_all():
    """The whole point. Before this, no invocation could target it."""
    d = planned("--target", "opensuse-tumbleweed", "--packages", "quickshell,cpptrace-devel")
    assert d["target"] == "opensuse-tumbleweed"
    assert len(d["build"]["include"]) == 4
    assert sorted(d["arches"]) == ["aarch64", "x86_64"]


def test_a_non_el10_target_publishes_to_its_contract_r2_path():
    factory = yaml.safe_load((ROOT / "manifests" / "package-factory.yaml").read_text(encoding="utf-8"))
    r2 = factory["targets"]["opensuse-tumbleweed"]["r2_path"]
    rows = publish_rows(planned("--target", "opensuse-tumbleweed", "--packages", "quickshell"))
    for arch, row in rows.items():
        assert row["src"] == r2.replace("{arch}", arch), (arch, row["src"])
        assert row["mirror"] == ""


def test_the_build_image_comes_from_the_contract():
    factory = yaml.safe_load((ROOT / "manifests" / "package-factory.yaml").read_text(encoding="utf-8"))
    for target in ("el10", "opensuse-tumbleweed"):
        d = planned("--target", target, "--packages", "quickshell" if target != "el10" else "libunwind-devel")
        assert d["image"] == factory["targets"][target]["probe_image"], target


def test_a_non_rpm_target_is_refused():
    """arch is pkg.tar.zst and needs repo-add, not createrepo_c. Publishing it
    through this workflow would produce a repository pacman cannot read."""
    result = run("--target", "arch", "--packages", "quickshell")
    assert result.returncode != 0
    assert "not rpm" in result.stderr


def test_a_package_that_does_not_target_it_is_refused():
    result = run("--target", "el10", "--packages", "dankcalendar")
    assert result.returncode != 0
    assert "does not target el10" in result.stderr


def test_an_unknown_target_is_refused():
    result = run("--target", "nosuchtarget", "--packages", "quickshell")
    assert result.returncode != 0
    assert "unknown target" in result.stderr


def test_an_arch_the_target_does_not_declare_is_refused():
    result = run("--target", "el10", "--packages", "libunwind-devel", "--arches", "s390x")
    assert result.returncode != 0
    assert "does not declare arch" in result.stderr


def test_empty_arches_means_every_arch_the_target_declares():
    """The workflow always passes --arches, and its input default is empty."""
    d = planned("--target", "el10", "--packages", "libunwind-devel", "--arches", "")
    assert sorted(d["arches"]) == ["aarch64", "x86_64"]


def test_each_arch_builds_and_verifies_on_its_own_native_runner():
    d = planned("--target", "opensuse-tumbleweed", "--packages", "quickshell")
    assert {r["arch"]: r["runner"] for r in d["build"]["include"]} == {
        "x86_64": "ubuntu-latest", "aarch64": "ubuntu-24.04-arm"}
    assert {r["arch"]: r["runner"] for r in d["publish"]["include"]} == {
        "x86_64": "ubuntu-latest", "aarch64": "ubuntu-24.04-arm"}


WORKFLOW = ROOT / ".github" / "workflows" / "publish-tideforge-rpms.yml"


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_no_job_still_hardcodes_el10():
    """The regression this change exists to prevent. `TARGET: el10`,
    `IMAGE: quay.io/centos/centos:stream10`, and either `case $arch` block
    reappearing means another target is silently unpublishable again."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "TARGET: el10" not in text
    assert "IMAGE: quay.io/centos/centos:stream10" not in text
    assert 'x86_64)  src="repo/10-stream-x86_64"' not in text
    assert 'x86_64)  url="https://repo.tunaos.org/repo/10/x86_64/"' not in text


def test_every_matrix_comes_from_the_plan_job():
    jobs = workflow()["jobs"]
    for name in ("build", "publish", "verify"):
        assert "needs.plan.outputs" in str(jobs[name]["strategy"]["matrix"]), name


def test_publish_and_verify_depend_on_plan():
    jobs = workflow()["jobs"]
    assert "plan" in jobs["publish"]["needs"]
    assert "plan" in jobs["verify"]["needs"]


def test_the_repo_wipe_guard_is_still_enforced_in_the_workflow():
    """The planner deciding min_rpms is useless if the workflow stops
    checking it."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "min_rpms" in text
    assert "refusing to risk a repo wipe" in text


def test_the_artifact_name_carries_the_target():
    """Two targets publishing in the same repo must not collide on artifact
    names, or a wave would download another target's RPMs and sync them into
    its own prefix."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "publish-rpm-${{ matrix.target }}-${{ matrix.arch }}-${{ matrix.package }}" in text
    assert "pattern: publish-rpm-${{ needs.plan.outputs.target }}-${{ matrix.arch }}-*" in text


def test_the_target_is_a_dispatch_input_defaulting_to_el10():
    spec = workflow()
    triggers = spec[True] if True in spec else spec["on"]
    target = triggers["workflow_dispatch"]["inputs"]["target"]
    assert target["default"] == "el10"
