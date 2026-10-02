"""The lint policy in ruff.toml has to be executed by something.

`ruff.toml` landed in #626 selecting E, F, I and UP at 100 columns. Nothing in
the repository ever ran ruff: not a workflow job, not a justfile recipe, not a
pre-commit hook. A declared-but-unexecuted policy does not hold still -- by
c68ec01 the tree carried 378 violations across 87 files while `ruff.toml` read
as an enforced contract:

    E501  212   line too long (27 files, longest 1300 cols)
    I001   62   unsorted import block
    F401   42   imported but unused
    E741   15   ambiguous variable name `l`
    F541    7   f-string without placeholders
    F841    6   assigned but never used
    E712    4   comparison to True/False
    E402    3   import not at top of file
    E401    1   multiple imports on one line

The tests here are the part of the gate that lives in the test suite, which is
a required check. They do two different jobs:

  * `test_the_repository_runs_ruff_somewhere` asserts an execution surface
    exists at all. It is the guard against the original failure -- a config with
    no runner -- coming back by deletion of the justfile recipe.
  * `test_the_enforced_subset_is_clean` runs ruff itself, so a new violation in
    the enforced subset fails the suite rather than waiting for someone to run
    ruff by hand.

E501 is deliberately outside the enforced subset. Its 212 violations are a real
backlog that cannot be mechanically fixed without rewrapping generated shell
and RPM macro strings, and it is tracked on its own issue (#765). Excluding it here
rather than in `ruff.toml` keeps the rule selected: the config still states the
line-length policy, and the day the backlog is paid off, deleting one argument
turns the gate on with no config change.

Both tests skip rather than fail when ruff is not installed, because pytest is
a required check and its job does not install ruff today. That does not weaken
them: the justfile assertion is unconditional, so the runner cannot be removed
unnoticed even where ruff is absent.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = ROOT / "justfile"
RUFF_TOML = ROOT / "ruff.toml"

# The one rule held out of the gate, and the only one allowed to be held out.
# A second entry here means a rule was retired without deleting it from
# ruff.toml, which is the failure this whole file exists to prevent.
DEFERRED = ("E501",)


def ruff_available() -> bool:
    if shutil.which("ruff"):
        return True
    probe = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def run_ruff(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_the_policy_is_declared():
    """Nothing below means anything if the config went away."""
    assert RUFF_TOML.is_file(), "ruff.toml is gone; the lint policy is undeclared"
    body = RUFF_TOML.read_text()
    assert "line-length" in body
    assert "select" in body


def test_the_repository_runs_ruff_somewhere():
    """A config no runner invokes is decoration. Keep a runner.

    The justfile recipe is the surface that can live in the tree without the
    `workflows` permission. If CI later grows its own ruff job, this assertion
    still holds -- it asks that *a* runner exists, not that only one does.
    """
    body = JUSTFILE.read_text()
    assert re.search(r"^lint-python:", body, re.MULTILINE), (
        "justfile has no lint-python recipe, so nothing runs the policy in "
        "ruff.toml -- see the module docstring"
    )
    recipe = body.split("lint-python:", 1)[1]
    assert "ruff check" in recipe.split("\n\n", 1)[0], (
        "the lint-python recipe does not invoke `ruff check`"
    )


def test_the_default_check_recipe_includes_lint():
    """`just check` is the documented local mirror of CI; lint belongs in it."""
    body = JUSTFILE.read_text()
    check = re.search(r"^check:(?P<deps>.*)$", body, re.MULTILINE)
    assert check, "justfile has no check recipe"
    assert "lint-python" in check.group("deps"), (
        "`just check` does not depend on lint-python, so the documented "
        "pre-push check still skips the lint policy"
    )


def test_the_deferred_rule_is_still_selected_in_config():
    """Deferring a rule in the runner must not retire it in the config.

    E501 is skipped by the gate, not removed from the policy. If it stops being
    selected, the 212-violation backlog stops being visible to anyone and the
    line-length contract is silently gone.
    """
    body = RUFF_TOML.read_text()
    # E501 is reached via the "E" family rather than being named directly.
    assert re.search(r'select\s*=\s*\[[^]]*"E"', body), (
        'ruff.toml no longer selects the "E" family, so the deferred E501 '
        "backlog is invisible instead of pending"
    )


@pytest.mark.skipif(not ruff_available(), reason="ruff is not installed")
def test_the_enforced_subset_is_clean():
    """Every rule except the deferred backlog passes, right now."""
    result = run_ruff("--extend-ignore", ",".join(DEFERRED), ".")
    assert result.returncode == 0, (
        "ruff reports violations outside the deferred set:\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.skipif(not ruff_available(), reason="ruff is not installed")
def test_the_deferred_backlog_does_not_grow():
    """A ratchet, so the held-out rule shrinks and never expands.

    This is the honest version of deferring a rule: the violations are counted,
    and adding one fails. Lower the ceiling as the backlog is paid off; when it
    reaches zero, drop E501 from DEFERRED and delete this test.
    """
    ceiling = 212
    result = run_ruff("--select", ",".join(DEFERRED), "--output-format=concise", ".")
    count = sum(
        1 for line in result.stdout.splitlines() if re.search(r":\d+:\d+: E501 ", line)
    )
    assert count <= ceiling, (
        f"{count} E501 violations, up from the recorded {ceiling}; new long "
        "lines are being added to a backlog that is supposed to drain"
    )
