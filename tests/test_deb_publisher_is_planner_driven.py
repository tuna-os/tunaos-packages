"""The deb publisher derives its matrix instead of hand-listing it (#479).

publish-tideforge-debs.yml was the last publisher with a literal `include:`.
publish-tideforge-rpms.yml resolves its matrix from the recipes in a plan job
and publish-build-chain-rpms.yml calls plan-build-chain-publish.py.

This is not a style complaint. A hand-listed matrix cannot report a MISSING
row -- a row nobody wrote looks exactly like a row nobody wanted -- which is
how every deb ever published turned out to be amd64: the matrix had no
`arch` key at all, and nothing anywhere said it should have one.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-tideforge-debs.yml"
PLANNER = ROOT / "scripts" / "plan-deb-publish.py"
WAVE = "cpptrace-devel,pop-icon-theme,cosmic-randr,cosmic-panel,cosmic-icon-theme"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PLANNER), *args], cwd=ROOT, capture_output=True, text=True)


def planned(*args: str) -> dict:
    result = run(*args)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_default_wave_reproduces_the_matrix_it_replaced():
    """The hand-written matrix was 5 packages x 2 distros x 2 arches = 20 rows.
    Deriving it must not quietly change WHAT gets published -- only where the
    list comes from."""
    d = planned("--packages", WAVE)
    assert d["count"] == 20
    rows = {(r["package"], r["distro"], r["arch"]) for r in d["matrix"]["include"]}
    assert len(rows) == 20
    assert {r[1] for r in rows} == {"ubuntu", "debian-sid"}
    assert {r[2] for r in rows} == {"amd64", "arm64"}


def test_arches_come_from_the_target_contract():
    """The point of the change: arches are the target's own `architectures`,
    the same list the gate builds, so the publisher cannot silently omit one
    the way it omitted arm64."""
    factory = yaml.safe_load((ROOT / "manifests" / "package-factory.yaml").read_text(encoding="utf-8"))
    for label, target in (("ubuntu", "ubuntu"), ("debian-sid", "debian")):
        declared = factory["targets"][target]["architectures"]
        d = planned("--packages", "pop-icon-theme", "--distros", label)
        assert sorted({r["arch"] for r in d["matrix"]["include"]}) == sorted(declared), label


def test_the_image_comes_from_the_target_contract():
    factory = yaml.safe_load((ROOT / "manifests" / "package-factory.yaml").read_text(encoding="utf-8"))
    d = planned("--packages", "pop-icon-theme")
    for row in d["matrix"]["include"]:
        target = "ubuntu" if row["distro"] == "ubuntu" else "debian"
        assert row["image"] == factory["targets"][target]["probe_image"], row


def test_each_arch_builds_on_its_own_native_runner():
    d = planned("--packages", "pop-icon-theme")
    runners = {r["arch"]: r["runner"] for r in d["matrix"]["include"]}
    assert runners == {"amd64": "ubuntu-latest", "arm64": "ubuntu-24.04-arm"}


def test_a_recipe_that_does_not_target_deb_is_refused():
    """evtest is el10-only. Publishing it to an apt repo would produce a
    package no target declares, discovered mid-wave rather than at plan time."""
    result = run("--packages", "evtest")
    assert result.returncode != 0
    assert "does not target ubuntu" in result.stderr


def test_a_nonexistent_recipe_is_refused():
    result = run("--packages", "nosuchpackage")
    assert result.returncode != 0
    assert "no recipe" in result.stderr


def test_an_arch_the_target_does_not_declare_is_refused():
    result = run("--packages", "pop-icon-theme", "--arches", "s390x")
    assert result.returncode != 0
    assert "does not declare arch" in result.stderr


def test_an_unknown_distro_label_is_refused():
    result = run("--packages", "pop-icon-theme", "--distros", "fedora")
    assert result.returncode != 0
    assert "unknown distro" in result.stderr


def test_the_distro_labels_are_the_live_repo_prefixes():
    """`ubuntu` and `debian-sid` are path segments of published repositories
    (r2:bluefin/tideforge/<distro>/, served at
    repo.tunaos.org/tideforge/<distro>/). Renaming one orphans a live repo, so
    the label set is pinned here rather than derived from the target names --
    the debian target's label is NOT `debian`."""
    d = planned("--packages", "pop-icon-theme")
    assert sorted({r["distro"] for r in d["matrix"]["include"]}) == ["debian-sid", "ubuntu"]
    assert {r["target"] for r in d["matrix"]["include"]} == {"ubuntu", "debian"}


def test_verify_covers_exactly_what_was_built():
    """Verifying an arch or distro the wave did not build fails a wave that
    did what it was asked; verifying fewer leaves a published repo unproven."""
    d = planned("--packages", WAVE)
    built = {(r["distro"], r["arch"]) for r in d["matrix"]["include"]}
    verified = {(r["distro"], r["arch"]) for r in d["verify_matrix"]["include"]}
    assert verified == built
    assert d["verify_count"] == 4


def test_a_narrowed_wave_narrows_publish_and_verify_too():
    """Hard-coded downstream matrices meant a one-distro wave would rclone
    sync an EMPTY tree over the other distro's live repository -- the #124
    repo-wipe shape."""
    d = planned("--packages", "pop-icon-theme", "--distros", "ubuntu")
    assert d["distros"] == ["ubuntu"]
    assert {r["distro"] for r in d["verify_matrix"]["include"]} == {"ubuntu"}


def test_no_job_still_hard_codes_a_distro_or_arch_list():
    """The regression this whole change exists to prevent."""
    jobs = workflow()["jobs"]
    for name in ("build", "publish", "verify"):
        matrix = jobs[name]["strategy"]["matrix"]
        assert "needs.plan.outputs" in str(matrix), (name, matrix)
        assert "include" not in (matrix if isinstance(matrix, dict) else {}), name


def test_publish_and_verify_depend_on_plan():
    """Reading needs.plan.outputs without needing plan is a silently empty
    matrix, not an error."""
    jobs = workflow()["jobs"]
    assert "plan" in jobs["publish"]["needs"]
    assert "plan" in jobs["verify"]["needs"]


def test_the_curated_default_wave_is_still_an_explicit_list():
    """Deriving the PACKAGE list from "every recipe targeting deb" would
    silently widen what ships. The planner validates the list; it must not
    invent it."""
    spec = workflow()
    triggers = spec[True] if True in spec else spec["on"]
    default = triggers["workflow_dispatch"]["inputs"]["packages"]["default"]
    assert default == WAVE


def test_the_exact_invocation_the_workflow_emits_works():
    """The workflow always passes --arches, and its default input is the EMPTY
    STRING. If empty meant "no arches" rather than "every arch the target
    declares", every dispatch would plan an empty matrix -- and the failure
    would land on a production publish, not in CI."""
    d = planned("--packages", WAVE, "--distros", "ubuntu,debian-sid", "--arches", "")
    assert d["count"] == 20
    assert d["arches"] == ["amd64", "arm64"]


def test_inputs_tolerate_the_whitespace_a_human_types():
    """These arrive from a dispatch form. ` ubuntu , ` must not become a
    distro named ' ubuntu ' that fails the label lookup."""
    d = planned("--packages", " pop-icon-theme , cosmic-randr ,", "--distros", " ubuntu , ", "--arches", " amd64 , ")
    rows = {(r["package"], r["distro"], r["arch"]) for r in d["matrix"]["include"]}
    assert rows == {("pop-icon-theme", "ubuntu", "amd64"), ("cosmic-randr", "ubuntu", "amd64")}


def test_an_empty_package_list_is_refused_rather_than_planned_empty():
    """An empty matrix would make build and publish no-ops, and the publish
    job would then rclone sync an empty tree over the live repository."""
    result = run("--packages", "")
    assert result.returncode != 0
    assert "no packages requested" in result.stderr
