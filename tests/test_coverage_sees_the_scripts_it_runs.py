"""Coverage has to see the subprocesses the tests actually drive.

Most scripts here are tested the way CI runs them -- as a subprocess:

    subprocess.run([sys.executable, str(PLANNER), "--packages", "evtest"])

A subprocess is a fresh interpreter, so without coverage's process-startup
hook the parent measures nothing of it. Measured on 6a3e0f2b, same suite, same
command, the only difference being whether COVERAGE_PROCESS_START was set:

    scripts/plan-chain-shards.py             0% -> 97%
    scripts/hummingbird-desktop-roots.py     0% -> 97%
    scripts/plan-deb-publish.py              0% -> 87%
    scripts/plan-arch-publish.py             0% -> 81%
    scripts/parse-build-order.py             0% -> 70%
    repo total                              59% -> 67%

The difference is not cosmetic. 0% is the signal that sends someone to write
tests that already exist, and it is the number a coverage floor would be
judged against -- a floor picked from the blind figure sits 8 points low.

`.coveragerc` (this commit) is the half that can live in the tree. The other
half is one `env:` line in the Python Tests job, which needs the `workflows`
permission to push; it is written out in the issue for a maintainer to apply.
The tests below therefore split in two:

  * unconditional -- `.coveragerc` asks for parallel data files, and this
    interpreter honours COVERAGE_PROCESS_START at all (that is coverage's
    `.pth` file, easy to lose to a packaging change and silent when lost);
  * conditional -- the moment the job runs with `--cov`, it must also export
    the variable and install the plugin, and any floor it enforces must be the
    number codecov.yml declares. Until then those skip, saying why.

A conditional test is not a placeholder: it is the guard that catches someone
turning coverage on and getting the blind number.
"""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
COVERAGERC = ROOT / ".coveragerc"
LINT = ROOT / ".github" / "workflows" / "lint.yml"
CODECOV = ROOT / "codecov.yml"
REQUIREMENTS = ROOT / "requirements-ci.txt"


@pytest.fixture(scope="module")
def pytest_job() -> dict:
    workflow = yaml.safe_load(LINT.read_text())
    assert "pytest" in workflow["jobs"], "lint.yml no longer has a pytest job"
    return workflow["jobs"]["pytest"]


def step(job: dict, name: str) -> dict:
    for s in job["steps"]:
        if s.get("name") == name:
            return s
    raise AssertionError(f"no step named {name!r} in the pytest job")


def cov_run(job: dict) -> str:
    """The pytest step's script, or skip if coverage is not on yet."""
    run = step(job, "Run pytest")["run"]
    if "--cov" not in run:
        pytest.skip("the Python Tests job does not measure coverage yet")
    return run


# ── Unconditional: the config, and the mechanism it depends on ──────────────

def test_coverage_data_is_written_per_process():
    """One data file per process, or the subprocesses race the parent."""
    cfg = configparser.ConfigParser()
    cfg.read(COVERAGERC)
    assert cfg.getboolean("run", "parallel") is True
    assert cfg.get("run", "source").strip() == "scripts"


def test_the_interpreter_honours_coverage_process_start(tmp_path):
    """The hook itself: a child process must leave coverage data behind.

    COVERAGE_PROCESS_START only does anything because coverage ships a `.pth`
    file that runs `coverage.process_startup()` at interpreter start. Lose that
    file to a packaging change and every config still reads correct while the
    numbers silently go back to 0%.
    """
    pytest.importorskip("coverage")

    rc = tmp_path / "cov.cfg"
    rc.write_text("[run]\nparallel = True\nsource = .\n")
    (tmp_path / "child.py").write_text("value = 1 + 1\nprint(value)\n")

    env = dict(os.environ, COVERAGE_PROCESS_START=str(rc))
    result = subprocess.run(
        [sys.executable, "child.py"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"
    assert list(tmp_path.glob(".coverage.*")), (
        "the child process left no coverage data: coverage's .pth startup hook "
        "is missing, so subprocess coverage is off no matter what CI exports"
    )


def test_the_ci_requirements_pin_the_coverage_plugin():
    """The Python Tests job installs requirements-ci.txt, so the plugin lives there."""
    pins = [
        line.split("==")[0].strip().lower()
        for line in REQUIREMENTS.read_text().splitlines()
        if "==" in line
    ]
    assert "pytest-cov" in pins, (
        "requirements-ci.txt does not pin pytest-cov, so `pytest --cov` in the "
        "Python Tests job fails with 'unrecognized arguments'"
    )


def test_the_declared_target_is_a_number_something_could_enforce():
    """codecov.yml's target is the floor CI should use; keep it readable."""
    declared = yaml.safe_load(CODECOV.read_text())
    target = str(declared["coverage"]["status"]["project"]["default"]["target"])
    assert target.rstrip("%").replace(".", "", 1).isdigit(), target


# ── Conditional: live the moment the job turns coverage on ──────────────────

def test_measuring_coverage_means_exporting_the_startup_hook(pytest_job):
    cov_run(pytest_job)
    env = step(pytest_job, "Run pytest").get("env", {})
    assert "COVERAGE_PROCESS_START" in env, (
        "the job measures coverage without COVERAGE_PROCESS_START, so every "
        "subprocess-driven script reports 0% -- see .coveragerc"
    )
    assert env["COVERAGE_PROCESS_START"].endswith(".coveragerc")


def test_measuring_coverage_means_installing_the_plugin(pytest_job):
    cov_run(pytest_job)
    install = step(pytest_job, "Install dependencies")["run"]
    if "-r requirements-ci.txt" in install:
        assert "pytest-cov==" in REQUIREMENTS.read_text()
    else:
        assert "pytest-cov" in install


def test_the_report_leaves_the_runner(pytest_job):
    """A number printed and thrown away is not a coverage report."""
    run = cov_run(pytest_job)
    assert "--cov-report" in run


def test_an_enforced_floor_matches_the_declared_target(pytest_job):
    """Two numbers for one policy is how a target becomes decoration."""
    run = cov_run(pytest_job)
    if "--cov-fail-under" not in run:
        pytest.skip("the job enforces no floor yet")

    declared = yaml.safe_load(CODECOV.read_text())
    target = str(declared["coverage"]["status"]["project"]["default"]["target"])
    assert f"--cov-fail-under={target.rstrip('%')}" in run
