"""A dropped dist-git connection must not cost the whole run.

Run 31266605500 imported 9 of 11 bootstrap packages and lost two to
`fatal: the remote end hung up unexpectedly`. The step exits 1 on any failure
and `Build tiers` is `skipped`, so nothing was built at all -- by a network
flake, on packages that exist and had imported fine minutes earlier.

Two packages in eleven is a ~18% per-clone failure rate. The full manifest is
1248 packages.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "import-fedora-distgit.py"

spec = importlib.util.spec_from_file_location("ifd", SCRIPT)
IFD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(IFD)


class FakeResult:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def runner_returning(*results):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return results[min(len(calls) - 1, len(results) - 1)]

    run.calls = calls
    return run


def test_transient_failure_is_retried_until_it_succeeds(tmp_path):
    run = runner_returning(
        FakeResult(128, "fatal: the remote end hung up unexpectedly"),
        FakeResult(0),
    )
    slept = []
    result = IFD.clone_with_retry(
        "python-hatchling", "rawhide", tmp_path / "co", 3, runner=run, sleeper=slept.append
    )
    assert result.returncode == 0
    assert len(run.calls) == 2
    # Spread across the top half of the interval (see backoff_delay), so this
    # asserts the bound rather than a single value.
    assert len(slept) == 1
    assert 1.0 <= slept[0] <= 2.0


def test_a_package_that_does_not_exist_is_not_retried(tmp_path):
    run = runner_returning(
        FakeResult(128, "remote: Repository not found\nfatal: repository not found")
    )
    result = IFD.clone_with_retry(
        "python-nope", "rawhide", tmp_path / "co", 3, runner=run, sleeper=lambda _: None
    )
    assert result.returncode == 128
    assert len(run.calls) == 1, "a missing package is deterministic; retrying it is waste"


def test_attempts_are_bounded(tmp_path):
    run = runner_returning(FakeResult(128, "fatal: the remote end hung up unexpectedly"))
    result = IFD.clone_with_retry(
        "python-flaky", "rawhide", tmp_path / "co", 3, runner=run, sleeper=lambda _: None
    )
    assert result.returncode == 128
    assert len(run.calls) == 3


def test_backoff_grows(tmp_path):
    run = runner_returning(FakeResult(128, "fatal: early EOF"))
    slept = []
    IFD.clone_with_retry(
        "python-flaky", "rawhide", tmp_path / "co", 4, runner=run, sleeper=slept.append
    )
    assert len(slept) == 3
    for attempt, delay in enumerate(slept, start=1):
        assert 2 ** (attempt - 1) <= delay <= 2 ** attempt
    assert slept[0] < slept[1] < slept[2], "each rung must still dominate the last"


def test_backoff_never_shortens_the_wait(tmp_path):
    """The floor is half the interval, so spreading never makes us more impatient.

    The ladder was chosen to be patient with a server that is asking us to slow
    down; full jitter would halve the average wait and undercut that.
    """
    for attempt in range(1, 6):
        assert IFD.backoff_delay(attempt, jitter=lambda a, _b: a) == 2 ** (attempt - 1)
        assert IFD.backoff_delay(attempt, jitter=lambda _a, b: b) == 2 ** attempt


def test_concurrent_retries_do_not_land_together(tmp_path):
    """The failure this exists to prevent.

    Clones run as one burst of --jobs, so a shed fails them together. A delay
    that is a pure function of the attempt number re-issues all of them at the
    same instant. Run 31270801603 lost 20 of 66 packages with their retries
    exhausted, in lockstep.
    """
    delays = {IFD.backoff_delay(1) for _ in range(200)}
    assert len(delays) > 1, "identical delays would re-synchronise the burst"


def test_a_503_is_transient(tmp_path):
    """The host answers 503 under load, and we are part of the load.

    Run 31268302766 needed retries on 8 of 11 clones; six recovered. Treating
    503 as permanent would have failed all eight.
    """
    assert not IFD.clone_is_permanent_failure(
        "fatal: unable to access 'https://src.fedoraproject.org/rpms/python-wheel.git/': "
        "The requested URL returned error: 503"
    )


def test_default_attempts_outlast_a_busy_host():
    """Three attempts over six seconds is too impatient for a 503.

    Two of eleven clones still failed at that setting after exhausting their
    retries, so the default has to be patient enough to ride out the window,
    not merely non-zero.
    """
    import argparse

    parser = argparse.ArgumentParser()
    src = SCRIPT.read_text()
    assert '"--clone-attempts", type=int, default=5' in src, (
        "default attempts dropped below 5; a 503 window outlasts three tries"
    )


def test_the_workflow_does_not_hammer_one_host():
    """Import concurrency is also how hard src.fedoraproject.org is pushed."""
    workflow = (REPO / ".github/workflows/build-hummingbird-desktops.yml").read_text()
    import re
    jobs = re.findall(r"import-fedora-distgit\.py.*?--jobs (\d+)", workflow, re.S)
    assert jobs, "could not find the import step's --jobs"
    assert int(jobs[0]) <= 4, (
        f"import runs {jobs[0]} concurrent clones against one host; it answers "
        "503 when pushed and the run is lost when a clone finally gives up"
    )


def test_partial_checkout_is_cleared_between_attempts(tmp_path):
    """git refuses to clone into a non-empty directory.

    Without this the retry fails with 'destination path already exists' and a
    transient error is laundered into a permanent one -- which would look like
    the retry is working while making the outcome strictly worse.
    """
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "partial").write_text("half a clone")
    seen = []

    def run(cmd, **kw):
        seen.append(checkout.exists() and any(checkout.iterdir()))
        return FakeResult(0) if len(seen) == 2 else FakeResult(128, "fatal: early EOF")

    IFD.clone_with_retry(
        "python-x", "rawhide", checkout, 3, runner=run, sleeper=lambda _: None
    )
    assert seen == [False, False], "clone was attempted into a dirty directory"


def test_attempts_of_one_means_no_retry(tmp_path):
    run = runner_returning(FakeResult(128, "fatal: early EOF"))
    IFD.clone_with_retry("p", "rawhide", tmp_path / "co", 1, runner=run, sleeper=lambda _: None)
    assert len(run.calls) == 1


def test_the_workflow_does_not_pin_attempts_to_one():
    workflow = (REPO / ".github/workflows/build-hummingbird-desktops.yml").read_text()
    assert "--clone-attempts 1" not in workflow


def test_permanent_marker_matching_is_case_insensitive():
    assert IFD.clone_is_permanent_failure("remote: Repository NOT FOUND")
    assert not IFD.clone_is_permanent_failure("fatal: the remote end hung up unexpectedly")
