"""A chain that cannot finish inside the job ceiling must ship what it built.

Every scheduled hummingbird-desktops run from 08-19 to 08-24 died at
`timeout-minutes: 360` -- reported as CANCELLED -- with every step after the
build skipped. Six hours of mock output reached only the resume partial;
validation, SBOM, attestation and the publish artifact never ran, so the
SERVED repo never gained a package however much the partials grew. The 08-24
x86_64 cell (job 97441135486) was killed at 5h59m29s still inside the python
bootstrap tiers.

The fix is a soft deadline INSIDE the ceiling: CHAIN_BUDGET_SECONDS. At the
package boundary the chain stops dispatching, drains its in-flight builds,
writes CHAIN_DEFERRED_MARKER, and exits 0 -- so everything downstream runs on
what DID build.

The one thing that must never happen: a deferred partial reaching the action
cache. A recorded ActionResult means "these inputs are fully built"; later
runs cache-hit on it and skip building, so a partial in the cache freezes the
chain at partial FOREVER. Both cache writes are gated on the marker, and the
tests below run the real script rather than grepping it -- a deadline that is
parsed but never fires would pass any textual check.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHAIN = ROOT / "scripts" / "build-chain.sh"
CELL = ROOT / ".github" / "workflows" / "package-factory-cell.yml"
RUNNER = ROOT / "scripts" / "run-package-factory-cell.sh"

def write_manifest(tmp_path) -> pathlib.Path:
    """Three tiny real packages: dry-run still resolves each spec on disk."""
    lines = ["tiers:"]
    for tier, names in (("layer-00", ("alpha", "beta")), ("layer-01", ("gamma",))):
        lines += [f"  - name: {tier}", "    packages:"]
        for name in names:
            pkg = tmp_path / "src" / "deps" / name
            pkg.mkdir(parents=True, exist_ok=True)
            (pkg / f"{name}.spec").write_text(
                f"Name: {name}\nVersion: 1\nRelease: 1\nSummary: t\n"
                "License: MIT\n%description\nt\n")
            # build-chain.sh roots manifest paths at the REPO checkout, so
            # hand it a path that walks from there to the fixture.
            lines.append(f"      - path: {os.path.relpath(pkg, ROOT)}")
    manifest = tmp_path / "order.yml"
    manifest.write_text("\n".join(lines) + "\n")
    return manifest


def run_chain(tmp_path, budget: str | None):
    manifest = write_manifest(tmp_path)
    marker = tmp_path / "chain-deferred"
    # Hermetic: ensure_local_repo shells out to createrepo_c even under
    # --dry-run, and CI/dev machines need not have it. Shimmed on PATH, the
    # same way this suite already fakes dpkg and find elsewhere.
    shims = tmp_path / "bin"
    shims.mkdir(exist_ok=True)
    stub = shims / "createrepo_c"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    env = {**os.environ, "CHAIN_DEFERRED_MARKER": str(marker),
           "PATH": f"{shims}:{os.environ['PATH']}"}
    env.pop("CHAIN_BUDGET_SECONDS", None)
    if budget is not None:
        env["CHAIN_BUDGET_SECONDS"] = budget
    proc = subprocess.run(
        ["bash", str(CHAIN), "--manifest", str(manifest), "--dist", ".fc43",
         "--local-repo", str(tmp_path / "repo"), "--dry-run"],
        env=env, capture_output=True, text=True, timeout=120, cwd=ROOT)
    return proc, marker


def test_budget_zero_defers_everything_and_still_exits_zero(tmp_path):
    proc, marker = run_chain(tmp_path, "0")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "deferring the rest" in proc.stdout
    assert "Packages built:  0" in proc.stdout
    assert "deferred" in proc.stdout.lower()
    assert marker.exists(), "no CHAIN_DEFERRED_MARKER written on deadline"
    body = marker.read_text()
    assert "deferred=3" in body, body


def test_no_budget_means_no_deadline_and_no_marker(tmp_path):
    """Unset budget must behave exactly as before this change existed."""
    proc, marker = run_chain(tmp_path, None)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "All packages built successfully!" in proc.stdout
    assert not marker.exists(), "a marker appeared without a deadline"


def test_a_generous_budget_defers_nothing(tmp_path):
    """The deadline must compare against elapsed time, not merely being set."""
    proc, marker = run_chain(tmp_path, "3600")
    assert proc.returncode == 0
    assert "All packages built successfully!" in proc.stdout
    assert not marker.exists()


def test_a_deferred_run_is_never_reported_as_fully_built(tmp_path):
    proc, _ = run_chain(tmp_path, "0")
    assert "All packages built successfully!" not in proc.stdout, (
        "a truncated chain announced itself complete -- the exact lie the "
        "desktop contract told for hummingbird images"
    )


# --- the cache guard ---------------------------------------------------------

def _steps():
    return yaml.safe_load(CELL.read_text())["jobs"]["build"]["steps"]


def _step(fragment):
    for step in _steps():
        if fragment in (step.get("name") or ""):
            return step
    raise AssertionError(f"no step named like {fragment!r}")


def test_both_cache_writes_are_gated_on_the_deferred_flag():
    for name in ("Record a new ActionResult", "Save validated output"):
        cond = _step(name).get("if", "")
        assert "chain_deferred" in cond, (
            f"{name!r} is not gated on chain_deferred: a deferred partial "
            "recorded in the action cache freezes the chain at partial forever"
        )


def test_the_flag_is_read_from_the_step_that_sets_it():
    build = _step("Build on an exact miss")
    assert build.get("id") == "build", (
        "the build step's id changed; every chain_deferred condition now "
        "reads an empty string and the guard is silently OFF"
    )


def test_the_runner_reports_deferral_through_github_output():
    text = RUNNER.read_text()
    assert "CHAIN_DEFERRED_MARKER" in text
    assert "chain_deferred=true" in text
    assert "CHAIN_BUDGET_SECONDS" in text


def test_the_budget_leaves_real_headroom_inside_the_ceiling():
    """Budget must sit meaningfully below timeout-minutes: 360, or the drain
    and the post-build steps die at the ceiling exactly as before."""
    text = RUNNER.read_text()
    import re
    m = re.search(r"CHAIN_BUDGET_SECONDS:-(\d+)", text)
    assert m, "no default budget in the runner"
    budget = int(m.group(1))
    ceiling = 360 * 60
    assert budget <= ceiling - 3600, (
        f"budget {budget}s leaves less than an hour of headroom under the "
        f"{ceiling}s job ceiling"
    )


def test_the_publish_artifact_is_not_gated_on_the_deferred_flag():
    """Shipping what built is the point; only the CACHE writes are gated."""
    for step in _steps():
        with_ = step.get("with") or {}
        name = str(with_.get("name", ""))
        if "publish-rpm" in name or "publish-" in name:
            assert "chain_deferred" not in (step.get("if") or ""), (
                "the publish artifact upload is gated on chain_deferred — "
                "that re-creates 'nothing publishes until a run completes'"
            )
