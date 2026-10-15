"""The dist-git clone retry in scripts/import-fedora-distgit.py.

src.fedoraproject.org throttles concurrent clones.  The import step runs
several at a time, and the server answers part of the batch with HTTP 503 or
drops the connection ("fatal: the remote end hung up unexpectedly") -- a
different subset of packages each time.  Run 31266178578 lost 17 of its 24
clones that way, and the rerun of the same commit lost 13, with only three
packages failing in both.  With one attempt per package that is a failed import
and a red job, for nothing that is wrong with the repository.

What a mistake here would break: retrying nothing leaves the flake in place,
and retrying everything turns a package that genuinely is not in dist-git (or a
branch that does not exist) into five attempts and a slow, misleading failure.
So both halves are pinned -- transient failures are retried, definitive "not
found" answers are not.

The retry also has to be shared.  Run 31270801603 still lost 20 of 66 imports
with per-package retries in place, because the server throttles the client
rather than the clone: every worker is refused at once, and retries that each
keep their own schedule pile straight back onto the limit that refused them.
`Throttle` is what makes the batch pause as one, so it is pinned here too.

The real network is not involved: a stub `git` earlier on PATH plays the
server, and records how many times each package was asked for.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "import-fedora-distgit.py"


def load_script():
    spec = importlib.util.spec_from_file_location("import_fedora_distgit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


importer = load_script()

# `git [-c k=v]... clone --depth 1 --branch <branch> <url> <dest>`. The
# destination is matched as the last argument rather than by position, so that
# adding another `-c` (the http.lowSpeed* pair that turns a stalled clone into
# a failure, say) does not quietly turn this stub into an unconditional exit 1
# and the tests below into assertions about nothing.
STUB_GIT = """#!/usr/bin/env bash
set -u
if [ "$1" = "-C" ]; then
    echo 0123456789abcdef0123456789abcdef01234567
    exit 0
fi
for arg in "$@"; do
    [ "$arg" = "clone" ] || continue
    dest="${@: -1}"
    pkg="$(basename "$dest")"
    attempts="$STUB_STATE/$pkg.attempts"
    printf 'x' >> "$attempts"
    n=$(wc -c < "$attempts")
    if [ -n "${STUB_FATAL:-}" ] && [ "$pkg" = "$STUB_FATAL" ]; then
        echo "fatal: Remote branch rawhide not found in upstream origin" >&2
        exit 128
    fi
    if [ "$n" -le "${STUB_FAIL_TIMES:-0}" ]; then
        echo "fatal: the remote end hung up unexpectedly" >&2
        exit 128
    fi
    mkdir -p "$dest"
    echo "Name: $pkg" > "$dest/$pkg.spec"
    exit 0
done
exit 1
"""


def run_import(tmp_path: Path, package: str, *, fail_times: int = 0, fatal: str = "") -> tuple[
    subprocess.CompletedProcess[str], int
]:
    """Import one package against the stub server.

    Returns the finished process and the number of clone attempts it made.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(STUB_GIT)
    (bindir / "git").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()

    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_STATE": str(state),
        "STUB_FAIL_TIMES": str(fail_times),
        "STUB_FATAL": fatal,
    }
    proc = subprocess.run(
        [
            "python3", str(SCRIPT),
            "--package", package,
            "--dest", str(tmp_path / "out"),
            "--state", str(tmp_path / "imports.json"),
            # The stub server is not throttling anyone; waiting on it would
            # only make the suite slow.
            "--clone-cooldown", "0",
            "--clone-pace", "0",
            "--retry-pass-delay", "0",
        ],
        capture_output=True, text=True, cwd=tmp_path, env=env,
    )
    attempts = state / f"{package}.attempts"
    return proc, len(attempts.read_bytes()) if attempts.exists() else 0


def test_transient_failure_is_retried_until_it_succeeds(tmp_path):
    proc, attempts = run_import(tmp_path, "cliphist", fail_times=2)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert attempts == 3
    assert "imported=1" in proc.stdout
    assert "failed=0" in proc.stdout
    # The import is only real if the packaging actually landed.
    assert (tmp_path / "out" / "cliphist" / "cliphist.spec").exists()
    assert json.loads((tmp_path / "imports.json").read_text())["cliphist"]["commit"]


def test_transient_failure_that_never_clears_still_fails_the_import(tmp_path):
    proc, attempts = run_import(tmp_path, "cliphist", fail_times=99)

    assert proc.returncode == 1
    # Five in the parallel pass, then five more in the serial pass over what
    # the parallel one could not clone. Bounded either way: the point is that
    # a package the server never serves ends the import instead of looping.
    assert attempts == 10, "retries are bounded, not endless"
    assert "FAILED cliphist:" in proc.stdout
    assert "failed=1" in proc.stdout


def test_a_package_that_does_not_exist_is_not_retried(tmp_path):
    proc, attempts = run_import(tmp_path, "not-a-package", fatal="not-a-package")

    assert proc.returncode == 1
    assert attempts == 1, "a missing package or branch is a manifest bug, not a flake"
    assert "FAILED not-a-package:" in proc.stdout


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def throttle(base=5.0, cap=60.0):
    clock = FakeClock()
    return importer.Throttle(base=base, cap=cap, clock=clock.time, sleep=clock.sleep), clock


def test_a_worker_that_was_not_refused_still_waits_out_the_cooldown():
    # The whole point: the server is throttling the client, so a clone that
    # would have gone out next only adds to the load that caused the refusal.
    gate, clock = throttle()

    gate.wait()
    assert clock.slept == [], "nothing has been refused yet; nobody waits"

    gate.penalise()
    gate.wait()
    assert len(clock.slept) == 1
    assert 5 <= clock.slept[0] <= 7, "the base cooldown, plus bounded jitter"


def test_a_second_casualty_of_the_same_wave_does_not_stack_the_pause():
    gate, clock = throttle()

    gate.penalise()
    clock.now += 1  # another worker is refused a moment later, same wave
    gate.penalise()

    gate.wait()
    assert 4 <= clock.slept[0] <= 6, "what is left of the first pause, not two of them"

    # ...and the wave counts once, so the next one is 10s and not 20s.
    gate.penalise()
    gate.wait()
    assert 10 <= clock.slept[1] <= 12


def test_each_fresh_wave_backs_off_further_up_to_the_cap():
    gate, clock = throttle(base=5.0, cap=20.0)

    for _ in range(4):
        gate.penalise()
        gate.wait()

    # Jitter adds up to 2s to each pause; the cap is on the pause, not on it.
    for expected, slept in zip([5.0, 10.0, 20.0, 20.0], clock.slept, strict=True):
        assert expected <= slept <= expected + 2, f"doubling, then capped: {clock.slept}"


def test_a_throttled_clone_waits_with_the_batch_and_not_on_its_own():
    """The shared cooldown replaces the per-worker backoff, it does not add to it.

    A worker that also kept its own 2/4/8s schedule would resume ahead of the
    batch and hit the limit that refused it, which is the failure `Throttle`
    exists to stop.
    """
    gate, clock = throttle()
    slept: list[float] = []
    calls: list[list[str]] = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 128, stdout="", stderr="fatal: the remote end hung up unexpectedly"
        )

    importer.clone_with_retry(
        "cliphist", "rawhide", Path("/nonexistent/co"), 3,
        runner=runner, sleeper=slept.append, throttle=gate,
    )

    assert len(calls) == 3
    assert slept == [], "the worker backed off privately as well as with the batch"
    # ...and it did wait: two refusals, each followed by the shared cooldown.
    assert len(clock.slept) == 2


def test_a_zero_cooldown_never_sleeps():
    # Only reachable via --clone-cooldown 0, which the tests use so a stub
    # server is not waited on.
    gate, clock = throttle(base=0.0)

    gate.penalise()
    gate.wait()
    assert clock.slept == []


def test_calibrate_jobs_scales_with_tier_size():
    # Small tiers afford higher concurrency.
    assert importer.calibrate_jobs(0) == 1
    assert importer.calibrate_jobs(1) == 1
    assert importer.calibrate_jobs(12) == 4
    assert importer.calibrate_jobs(24) == 4

    # Moderate/growing tiers scale down to protect src.fedoraproject.org (#614).
    assert importer.calibrate_jobs(25) == 3
    assert importer.calibrate_jobs(49) == 3  # kde-00
    assert importer.calibrate_jobs(66) == 3  # niri-00
    assert importer.calibrate_jobs(100) == 3

    # Large manifests scale down further to prevent sustained load shedding.
    assert importer.calibrate_jobs(101) == 2
    assert importer.calibrate_jobs(309) == 2  # gnome-* tiers
    assert importer.calibrate_jobs(599) == 2  # full desktop graph


def test_calibrate_jobs_honours_explicit_jobs_request():
    assert importer.calibrate_jobs(66, requested=8) == 8
    assert importer.calibrate_jobs(309, requested=4) == 4
    assert importer.calibrate_jobs(10, requested=1) == 1
    assert importer.calibrate_jobs(10, requested=0) == 1


def test_pacer_spaces_consecutive_launches():
    clock = FakeClock()
    pacer = importer.Pacer(interval=0.25, clock=clock.time, sleep=clock.sleep)

    pacer.pace()
    assert clock.slept == [], "first launch should start immediately"

    # Next launch at the same instant must wait interval seconds
    pacer.pace()
    assert clock.slept == [0.25]

    # Third launch at the same instant must wait another interval seconds
    pacer.pace()
    assert clock.slept == [0.25, 0.25]


def test_pacer_after_delay_launches_without_unnecessary_sleep():
    clock = FakeClock()
    pacer = importer.Pacer(interval=0.25, clock=clock.time, sleep=clock.sleep)

    pacer.pace()
    assert clock.slept == []

    # Advance clock past interval
    clock.now += 1.0
    pacer.pace()
    assert clock.slept == [], "launching after interval has passed should not sleep"


def test_pacer_zero_interval_never_sleeps():
    clock = FakeClock()
    pacer = importer.Pacer(interval=0.0, clock=clock.time, sleep=clock.sleep)

    for _ in range(5):
        pacer.pace()
    assert clock.slept == []


def test_clone_with_retry_calls_pacer():
    clock = FakeClock()
    pacer = importer.Pacer(interval=0.25, clock=clock.time, sleep=clock.sleep)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    importer.clone_with_retry(
        "cliphist", "rawhide", Path("/nonexistent/co"), 1,
        runner=runner, sleeper=lambda _: None, pacer=pacer,
    )
    assert len(calls) == 1
    assert clock.slept == []

    # Second clone through the same pacer at the same instant gets paced
    importer.clone_with_retry(
        "niri", "rawhide", Path("/nonexistent/co"), 1,
        runner=runner, sleeper=lambda _: None, pacer=pacer,
    )
    assert len(calls) == 2
    assert clock.slept == [0.25]

