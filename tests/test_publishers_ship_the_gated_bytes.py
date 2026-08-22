"""What a publisher ships must be what the gate approved.

Before #481 all three publishers rebuilt every cell from scratch inside the
publish workflow. The cost was the visible half -- the action cache is
measurably effective since #472, 14/14 hits at 171-382 KB in run 32568880740,
so a wave paid full build price for artifacts sitting in the cache.

The correctness half is larger. An ActionResult keyed on immutable inputs
exists so that the bytes which passed verify and smoke are the same object
that later gets published. A publisher that rebuilds produces a SECOND,
independently built set and ships those instead, and nothing anywhere compares
them.

It also supplies, for free, the guarantee #479 proposal 2 wanted from a new
mechanism. In package-factory-cell.yml `Save validated output` runs strictly
after `Validate package installation and smoke contract`, so a failed verify
never reaches the save. An entry's EXISTENCE is therefore proof the package
built and passed verify and smoke -- and a publisher that restores rather than
rebuilds inherits that proof.

The two ways this can be wired up wrong, both silent:

  cell id     actions/cache extracts a hit to the paths the SAVE recorded, not
              to whatever path the restore step names. A publisher using its
              own `publish-...` prefix would unpack the gate's directory,
              report a hit, and then build into its own -- so the workflow
              would claim a cache hit on every run while rebuilding every time.

  epoch       SOURCE_DATE_EPOCH is both baked into the built packages and an
              input to the action key. The publishers used %ct (committer
              date) where the gate uses %at (author date). Those differ for the
              same tree after any rebase, cherry-pick, amend or squash-merge,
              which is this repo's merge convention (#477) -- so the publisher
              could not hit the gate's entry even in principle, and when it
              rebuilt it produced different bytes than the gate approved.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "tideforge-cached-cell" / "action.yml"
CELL = ROOT / ".github" / "workflows" / "package-factory-cell.yml"
PUBLISHERS = {
    "rpm": ROOT / ".github" / "workflows" / "publish-tideforge-rpms.yml",
    "deb": ROOT / ".github" / "workflows" / "publish-tideforge-debs.yml",
    "arch": ROOT / ".github" / "workflows" / "publish-tideforge-arch.yml",
}
PLANNERS = {
    "rpm": ("plan-rpm-publish.py", ["--target", "el10", "--packages", "greetd"]),
    "deb": ("plan-deb-publish.py", ["--distro", "ubuntu", "--packages", "cli11-devel"]),
    "arch": ("plan-arch-publish.py", ["--packages", "greetd"]),
}


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def build_job(path: Path) -> dict:
    return load(path)["jobs"]["build"]


def steps_using(path: Path, action: str) -> list[dict]:
    return [
        step
        for job in load(path)["jobs"].values()
        for step in job.get("steps", [])
        if action in (step.get("uses") or "")
    ]


def test_the_action_exists():
    assert ACTION.is_file()


def test_every_publisher_uses_it():
    """One implementation, not three drifting copies -- the deb one had already
    drifted into a different build path entirely."""
    for name, path in PUBLISHERS.items():
        assert steps_using(path, "tideforge-cached-cell"), name


def test_no_publisher_still_builds_by_hand():
    """A leftover direct call to the cell runner or to dpkg-buildpackage means
    a second, uncached build path survived the conversion."""
    for name, path in PUBLISHERS.items():
        for job in load(path)["jobs"].values():
            for step in job.get("steps", []):
                run = step.get("run") or ""
                assert "run-package-factory-cell.sh" not in run, f"{name}: {step.get('name')}"
                assert "dpkg-buildpackage" not in run, f"{name}: {step.get('name')}"


def test_the_publishers_pass_the_gates_cell_id():
    """Not a cosmetic name. actions/cache restores to the paths the save
    recorded, so an invented prefix reports a hit and rebuilds anyway."""
    for name, path in PUBLISHERS.items():
        for step in steps_using(path, "tideforge-cached-cell"):
            assert step["with"]["cell-id"] == "${{ matrix.cell_id }}", name


def test_the_planners_emit_the_shared_cell_id():
    sys.path.insert(0, str(ROOT / "scripts"))
    import factory_contract

    for name, (script, args) in PLANNERS.items():
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), *args],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"
        payload = json.loads(result.stdout)
        rows = payload["build"]["include"] if "build" in payload else payload["matrix"]["include"]
        assert rows, name
        for row in rows:
            expected = factory_contract.tideforge_cell_id(
                row["package"], row.get("target", "arch"), row["arch"]
            )
            assert row["cell_id"] == expected, (name, row)


def test_the_gate_and_the_publishers_agree_on_the_formula():
    """The planner that feeds the gate must derive its id from the same
    function, or the two can diverge without any test noticing."""
    text = (ROOT / "scripts" / "plan-package-factory.py").read_text(encoding="utf-8")
    assert "factory_contract.tideforge_cell_id(" in text
    assert 'f"tideforge-{package}-{target_id}-{architecture}"' not in text


def test_the_epoch_is_the_author_date_everywhere():
    """%ct changes on every history rewrite for an unchanged tree; %at does
    not. The gate settled on %at in #477 and the publishers never followed."""
    assert "--format=%at" in ACTION.read_text(encoding="utf-8")
    for name, path in PUBLISHERS.items():
        assert "--format=%ct" not in path.read_text(encoding="utf-8"), name


def test_the_action_restores_the_same_paths_the_gate_saves():
    """A restore list narrower than the save list silently drops a build
    product some verify path reads -- which is exactly how package-info.txt
    broke every Arch hit once keys became stable (#472/#477)."""
    action = load(ACTION)
    cell = load(CELL)

    def cache_paths(spec: dict, action_name: str) -> list[set[str]]:
        steps = (
            spec["runs"]["steps"] if "runs" in spec
            else [s for j in spec["jobs"].values() for s in j.get("steps", [])]
        )
        return [
            {line.split("/", 2)[-1] for line in step["with"]["path"].strip().splitlines()}
            for step in steps
            if action_name in (step.get("uses") or "")
        ]

    gate = cache_paths(cell, "tideforge-action-cache")
    ours = cache_paths(action, "tideforge-action-cache")
    assert len(ours) == 2, "the action must both restore and save"
    assert gate, "the cell workflow no longer uses the action cache"
    for got in ours:
        assert got == gate[0], (got, gate[0])


def test_it_saves_only_after_validating():
    """The ordering is the whole guarantee: an entry exists only if verify and
    smoke passed, so restoring one is inheriting that proof (#479 proposal 2).
    Saving before validation would turn the cache into a record of things that
    merely compiled."""
    steps = load(ACTION)["runs"]["steps"]
    names = [step.get("name") or step.get("uses", "") for step in steps]
    save = next(
        i for i, step in enumerate(steps)
        if "tideforge-action-cache" in (step.get("uses") or "")
        and step.get("with", {}).get("operation") == "save"
    )
    validate = names.index("Validate package installation and smoke contract")
    assert validate < save, names


def test_validation_runs_on_a_hit_too():
    """Restored bytes are bytes the gate approved against the package universe
    of that day. Whether they still install today is a different question, and
    the one #179 is about."""
    steps = load(ACTION)["runs"]["steps"]
    validate = next(
        step for step in steps
        if step.get("name") == "Validate package installation and smoke contract"
    )
    assert "if" not in validate, "verify must not be skipped on a cache hit"


def test_building_is_conditional_on_a_miss():
    steps = load(ACTION)["runs"]["steps"]
    build = next(step for step in steps if step.get("name") == "Build on a genuine miss")
    assert build["if"] == "steps.verdict.outputs.hit != 'true'"


def test_the_hit_rate_is_reported_not_inferred():
    """#481 exists because the hit rate had been asserted from reasoning rather
    than measured. A ::notice lands in the run annotations, so publish-side
    hits are greppable."""
    text = ACTION.read_text(encoding="utf-8")
    assert "::notice title=action-cache::" in text


def test_the_deb_publisher_now_runs_the_clean_install_verify():
    """It ran lint-generated-deb.sh and stopped, while rpm and arch both ran
    verify-package-factory-cell.sh. Debs reached the bucket without the #179
    check ever running on the publish path."""
    assert "verify-package-factory-cell.sh" in ACTION.read_text(encoding="utf-8")
    assert steps_using(PUBLISHERS["deb"], "tideforge-cached-cell")


def test_the_deb_upload_refuses_an_empty_pool():
    """publish downloads the pool and rclone-syncs it, and sync makes the
    destination match the source (#124)."""
    upload = [
        step
        for step in build_job(PUBLISHERS["deb"])["steps"]
        if "upload-artifact" in (step.get("uses") or "")
    ]
    assert upload and upload[0]["with"]["if-no-files-found"] == "error"


def test_the_deb_build_is_not_shallow():
    """`git log -1 --format=%at -- <recipe dir>` cannot be answered from a
    shallow clone, and a wrong epoch is a wrong action key."""
    checkout = next(
        step for step in build_job(PUBLISHERS["deb"])["steps"]
        if "actions/checkout" in (step.get("uses") or "")
    )
    assert checkout["with"]["fetch-depth"] == 0
