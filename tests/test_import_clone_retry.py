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
    assert slept == [2]


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
    assert slept == [2, 4, 8]


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


def test_a_stalled_clone_becomes_a_retry_not_a_hang(tmp_path):
    """git has no default timeout for a connection that stalls.

    The retry only helps a clone that *fails*. A server that accepts the
    connection and then stops feeding it leaves git waiting forever: the step
    hangs, the retry never fires, and the job burns its 360-minute timeout
    having built nothing. The trunk import sat in exactly that state for an
    hour on 309 packages; expected was four to eight minutes.
    """
    calls = []

    def run(cmd, **kw):
        calls.append(kw.get("timeout"))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
        return FakeResult(0)

    result = IFD.clone_with_retry(
        "python-stalled", "rawhide", tmp_path / "co", 3,
        runner=run, sleeper=lambda _: None, timeout=5,
    )
    assert result.returncode == 0, "a stalled clone must be retried, not propagated"
    assert calls == [5, 5], "the timeout must be passed to every attempt"


def test_a_stall_that_never_clears_ends_as_a_normal_failure(tmp_path):
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)

    result = IFD.clone_with_retry(
        "python-stalled", "rawhide", tmp_path / "co", 2,
        runner=run, sleeper=lambda _: None, timeout=5,
    )
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_clone_asks_git_to_give_up_on_a_dead_transfer(tmp_path):
    """lowSpeedLimit/lowSpeedTime, so git itself aborts a stalled transfer."""
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return FakeResult(0)

    IFD.clone_with_retry("p", "rawhide", tmp_path / "co", 1, runner=run,
                         sleeper=lambda _: None)
    cmd = seen["cmd"]
    assert "http.lowSpeedLimit=1000" in cmd
    assert "http.lowSpeedTime=30" in cmd
    assert cmd.index("-c") < cmd.index("clone"), (
        "-c options must come before the subcommand or git rejects them"
    )


# --- the serial second pass -------------------------------------------------
#
# Per-clone retry cannot carry a large import on its own. Run 31271496131
# imported 258 of 263 and still lost the whole run, because the step exits 1 on
# any failure and `Build tiers` is then skipped. At a 2% residual failure rate a
# 263-package import almost never comes out clean; the manifest is 1248.

def test_serial_pass_is_the_documented_shape():
    """After the parallel pass, survivors are retried serially after a wait."""
    src = SCRIPT.read_text()
    assert "failed_first" in src, "no second pass over the parallel pass's failures"
    assert "retry_pass_delay" in src, "second pass does not wait before retrying"
    i = src.index("outcomes = list(pool.map(clone_one, pending))")
    j = src.index("failed_first")
    assert i < j, "the serial pass must come after the parallel one"


def test_second_pass_skips_packages_that_do_not_exist():
    """A missing package is deterministic; a cooldown will not conjure it."""
    src = SCRIPT.read_text()
    seg = src[src.index("failed_first = ["):src.index("if failed_first:")]
    assert "clone_is_permanent_failure" in seg, (
        "the second pass retries permanent failures, spending the cooldown on "
        "a package that is not there"
    )


def test_second_pass_results_replace_the_first_by_package_name():
    src = SCRIPT.read_text()
    seg = src[src.index("if failed_first:"):src.index("for (package, relative, target), clone in outcomes:")]
    assert "retried.get(item[0]" in seg, (
        "second-pass results must be keyed by package name; anything "
        "identity-based silently drops them"
    )
