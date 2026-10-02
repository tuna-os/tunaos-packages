#!/usr/bin/env python3
"""Import Fedora dist-git RPM packaging into src/hummingbird.

Only packaging inputs are copied (specs, patches, source declarations and
auxiliary build files); the dist-git repository itself is never nested in this
repository.  The result is reviewable and can be built by build-chain.sh.

Two drivers:

  * the desktop catalog's `fedora_distgit:` sources (the original behaviour), and
  * a build-order manifest's `distgit:` keys, which is how the measured
    Hummingbird desktop graph is materialised — 599 of its 670 packages are
    unmodified Fedora Rawhide packaging and are imported rather than vendored.

Hummingbird's own RPM project (gitlab.com/redhat/hummingbird, ci/dist_git.py)
works the same way: >95% of its packages are auto-imported from Fedora dist-git,
tracked in a JSON state file, and carry a Release bumped by 0.1 so a downstream
rebuild sorts above the pristine Fedora build without colliding with it.  This
script mirrors that: --state records the dist-git commit each package came
from, and --release-bump applies the 0.1 convention.

Per Hummingbird convention no %changelog entry is added for the downstream
change; the commit message carries it.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time

import yaml

RELEASE = re.compile(r"^(Release:\s*)(\d+)(%\{\?dist\}.*)$", re.MULTILINE)

# Retrying on a per-package schedule is not enough on its own: the server does
# not throttle one clone, it throttles the client, so the whole batch is
# refused at once and independent retries land back inside the same window and
# add to the load that caused it.  Run 31270801603 lost 20 of 66 imports that
# way, with four attempts each already in place.  So the backoff is shared: the
# first throttled clone parks every worker, and each fresh wave of throttling
# doubles the pause.
COOLDOWN_BASE = 5.0
COOLDOWN_CAP = 60.0

# Launch pacing flattens the initial and inter-task burst of clone requests (#614).
# Even with retries in place, starting N workers simultaneously slams
# src.fedoraproject.org with N concurrent git handshakes, provoking 503s and
# connection drops. Staggering clone launches ensures arrival peaks are smoothed.
DEFAULT_PACE = 0.25


class Throttle:
    """A cooldown shared by every clone worker.

    `penalise()` is called by whichever worker the server refused; it parks
    *all* of them until the cooldown expires, so a throttled batch retries as
    one quiet pause instead of as N independent retries that keep the client
    over the limit.  Repeated waves back off further, up to `cap`.
    """

    def __init__(
        self,
        base: float = COOLDOWN_BASE,
        cap: float = COOLDOWN_CAP,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep
        self._cap = cap
        self._delay = base
        self._until = 0.0

    def wait(self) -> None:
        with self._lock:
            remaining = self._until - self._clock()
        if remaining > 0:
            # Jittered, so the clones parked together do not all resume in the
            # same instant and get throttled together again.
            self._sleep(remaining + random.uniform(0, 2))

    def penalise(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._until:
                # Already cooling down; this is another casualty of the same
                # wave, not a sign that the wait should be longer still.
                return
            self._until = now + self._delay
            self._delay = min(self._delay * 2, self._cap)


class Pacer:
    """Ensures at least `interval` seconds elapse between consecutive clone starts.

    Without pacing, N workers launch N simultaneous git clones against
    src.fedoraproject.org at t=0, producing an instant burst that triggers HTTP 503
    or TCP connection resets ('fatal: the remote end hung up unexpectedly').
    Staggering launches by `interval` flattens the arrival peak while adding
    only a fraction of a second per clone to total run time (#614).
    """

    def __init__(
        self,
        interval: float = DEFAULT_PACE,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self._lock = threading.Lock()
        self._interval = max(0.0, float(interval))
        self._clock = clock
        self._sleep = sleep
        self._last_launch = -self._interval

    def pace(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = self._clock()
            target = max(now, self._last_launch + self._interval)
            self._last_launch = target
            wait_time = target - now
        if wait_time > 0:
            self._sleep(wait_time)


def calibrate_jobs(count: int, requested: int | None = None) -> int:
    """Scale clone concurrency to the batch size.

    A small batch (<= 24 packages, like original niri-00) can afford 4 parallel
    workers. A growing tier (25-100 packages, e.g. kde-00 at 49 or niri-00 at 66)
    sheds load under high concurrency; scaling down to 3 workers flattens peak
    demand against src.fedoraproject.org (#614). A large tier (> 100 packages,
    e.g. gnome-* at 309 or a full manifest) drops to 2 workers to keep peak load
    sustainable.

    If an explicit --jobs is requested, it is respected.
    """
    if requested is not None:
        return max(1, requested)
    if count <= 0:
        return 1
    if count <= 24:
        return min(count, 4)
    if count <= 100:
        return 3
    return 2


# A dist-git clone that fails is usually src.fedoraproject.org refusing or
# dropping the connection, not a package that does not exist:
#
#   FAILED python-hatchling: fatal: the remote end hung up unexpectedly
#   FAILED python-hatch-fancy-pypi-readme: fatal: the remote end hung up unexpectedly
#   imported=9 skipped=0 failed=2
#
# (run 31266605500). The step exits 1 on any failure and `Build tiers` is
# skipped, so two dropped connections cost the whole run. Three consecutive
# dispatches were lost this way before anything was built.
#
# The host also returns 503 under load, and we are part of that load -- earlier
# calibrations ran --jobs 8, all against one server. Run 31268302766 with
# retries on:
#
#   Retrying python-wheel (1/2): ... The requested URL returned error: 503
#   Retrying python-editables (2/2): ... The requested URL returned error: 503
#   imported=8 skipped=0 failed=2
#
# Eight of eleven clones needed a retry and six of them recovered, so retrying
# is right; three attempts over six seconds is just too impatient for a server
# that is asking us to slow down. Hence five attempts, a batch that waits out
# each refusal together (Throttle), and launch pacing + concurrency scaling
# (Pacer, calibrate_jobs) rather than unconstrained bursts.
PERMANENT_CLONE_ERRORS = ("not found", "does not exist", "could not read username")


def backoff_delay(attempt: int, jitter=None) -> float:
    """`2 ** attempt`, spread over the top half of that interval.

    The spread is the point, not a refinement.  The clones run as one burst of
    --jobs, so when the server sheds load they fail *together* -- and a delay
    that is a pure function of the attempt number re-issues every one of them
    at the same instant, rebuilding the burst that caused the failure.  Drawing
    each retry independently spreads the arrivals across the window.

    Run 31270801603 is what this is for: `niri-00` is 66 packages since #271
    regenerated the manifest, and 20 of them still failed *after their retries
    were exhausted*, in lockstep.  More attempts against a synchronised burst
    mostly buys a longer red.

    The floor is half the interval rather than zero, so this only ever spreads
    the wait and never shortens it below `2 ** (attempt - 1)`.  The ladder was
    chosen to be patient with a server asking us to slow down, and full jitter
    would halve the average wait and undercut that.
    """
    jitter = jitter or random.uniform
    return jitter(0.5, 1.0) * (2 ** attempt)


def clone_is_permanent_failure(stderr: str) -> bool:
    """True when retrying cannot help -- the package is not there.

    Everything else is treated as transient. Getting this wrong in the
    permanent direction is much worse than in the transient direction: a
    retried 404 wastes seconds, while a non-retried flake wastes the run.
    """
    lowered = stderr.lower()
    return any(marker in lowered for marker in PERMANENT_CLONE_ERRORS)


def clone_with_retry(
    package,
    branch,
    checkout,
    attempts=3,
    runner=None,
    sleeper=None,
    throttle=None,
    pacer=None,
    timeout=180,
    jitter=None,
):
    """Clone one package, retrying a refused or dropped connection.

    With a `throttle` the waiting is shared: the worker parks on the batch's
    cooldown before every attempt and reports each refusal to it, instead of
    keeping a private backoff schedule that would land back inside the window
    that refused it. With a `pacer`, clone launches are staggered across workers
    to prevent burst arrival peaks. Without them -- a single clone, or a unit
    test -- the worker backs off on its own, on the spread ladder in
    `backoff_delay`.
    """
    runner = runner or subprocess.run
    sleeper = sleeper or time.sleep
    url = f"https://src.fedoraproject.org/rpms/{package}.git"
    result = None
    for attempt in range(1, max(1, attempts) + 1):
        # Nothing is asked of the server while it is refusing clones,
        # including this worker's first attempt: joining a throttled wave only
        # prolongs it.
        if throttle is not None:
            throttle.wait()
        if pacer is not None:
            pacer.pace()
        # git refuses to clone into an existing non-empty directory, so a
        # partial checkout left by a failed attempt would turn one transient
        # error into a permanent one.
        if checkout.exists():
            shutil.rmtree(checkout, ignore_errors=True)
        try:
            result = runner(
                ["git",
                 # A retry only helps a clone that FAILS. git has no default
                 # timeout for one that STALLS, so a connection the server
                 # accepts and then stops feeding blocks forever -- the step
                 # hangs, the retry never fires, and the job burns its
                 # 360-minute timeout having built nothing. The trunk import
                 # sat here for an hour on 309 packages against a
                 # src.fedoraproject.org that was returning 503s; expected
                 # was four to eight minutes.
                 #
                 # These turn a stall into an ordinary failure, which the
                 # retry above already knows what to do with. 1000 B/s for
                 # 30s is well under any real transfer and well over a dead
                 # one.
                 "-c", "http.lowSpeedLimit=1000",
                 "-c", "http.lowSpeedTime=30",
                 "clone", "--depth", "1", "--branch", branch, url,
                 str(checkout)],
                capture_output=True, text=True,
                # Belt and braces: lowSpeedTime does not cover a connection
                # that hangs before the transfer begins.
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # A timeout that propagates kills the whole import; the point of
            # the timeout is to produce a retryable failure, not a new way to
            # lose the run.
            result = subprocess.CompletedProcess(
                args=["git", "clone", url],
                returncode=124,
                stdout="",
                stderr=f"timed out after {timeout}s",
            )
        if result.returncode == 0:
            return result
        if clone_is_permanent_failure(result.stderr or ""):
            return result
        if throttle is not None:
            throttle.penalise()
        if attempt < max(1, attempts):
            tail = (result.stderr or "").strip().splitlines()[-1:] or ["clone failed"]
            print(f"Retrying {package} ({attempt}/{attempts - 1}): {tail[0]}")
            if throttle is None:
                sleeper(backoff_delay(attempt, jitter))
    return result


def catalog_packages(catalog: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    data = yaml.safe_load(catalog.read_text())
    result: list[tuple[str, pathlib.Path]] = []
    seen: set[str] = set()
    for desktop in data["desktops"].values():
        for source in desktop["sources"]:
            package = source.get("fedora_distgit")
            if package and package not in seen:
                seen.add(package)
                result.append((package, pathlib.Path("src/hummingbird") / package))
    return result


def build_order_packages(
    manifest: pathlib.Path, tiers: list[str] | None = None
) -> list[tuple[str, pathlib.Path]]:
    data = yaml.safe_load(manifest.read_text())
    known = {tier["name"] for tier in data.get("tiers", [])}
    if tiers:
        unknown = sorted(set(tiers) - known)
        if unknown:
            raise SystemExit(f"no such tier(s) in {manifest}: {unknown}")
    result: list[tuple[str, pathlib.Path]] = []
    seen: set[str] = set()
    for tier in data.get("tiers", []):
        if tiers and tier["name"] not in tiers:
            continue
        for package in tier.get("packages", []):
            name = package.get("distgit")
            if name and name not in seen:
                seen.add(name)
                result.append((name, pathlib.Path(package["path"])))
    return result


def bump_release(specdir: pathlib.Path) -> str | None:
    """Release: 3%{?dist} -> Release: 3.1%{?dist}.

    Sorts above the pristine Fedora build (3.1 > 3) so a rebuilt package is
    never shadowed by a Fedora one that leaks into the same transaction, and is
    identifiable at a glance in `rpm -qa`.
    """
    for spec in sorted(specdir.glob("*.spec")):
        text = spec.read_text()
        bumped, count = RELEASE.subn(r"\g<1>\g<2>.1\g<3>", text, count=1)
        if count:
            spec.write_text(bumped)
            return spec.name
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", type=pathlib.Path, nargs="?")
    parser.add_argument(
        "--build-order", type=pathlib.Path,
        help="Import every `distgit:` entry of a build-order manifest.",
    )
    parser.add_argument(
        "--tier", action="append", dest="tiers",
        help="Restrict --build-order to these tiers. Repeatable. Importing "
             "the whole manifest is 599 dist-git clones; a tiered run needs "
             "only its own.",
    )
    parser.add_argument("--branch", default="rawhide")
    parser.add_argument("--package", action="append", dest="packages")
    parser.add_argument("--dest", type=pathlib.Path, default=pathlib.Path("src/hummingbird"))
    parser.add_argument(
        "--state", type=pathlib.Path,
        help="JSON file recording the dist-git commit each package came from.",
    )
    parser.add_argument(
        "--release-bump", action="store_true",
        help="Apply Hummingbird's +0.1 Release convention to the imported spec.",
    )
    parser.add_argument(
        "--jobs", type=int, default=None,
        help="Parallel dist-git clones. Defaults to auto-calibrating based on "
             "tier size (4 for <=24 pkgs, 3 for <=100 pkgs, 2 for >100 pkgs). "
             "Clones all hit one host (src.fedoraproject.org), so concurrency is "
             "scaled to avoid triggering server-side load shedding.",
    )
    parser.add_argument(
        "--clone-pace", "--clone-interval", type=float, default=DEFAULT_PACE,
        dest="clone_pace", metavar="SECONDS",
        help="Minimum seconds between consecutive clone launches (default: 0.25). "
             "Flattens the burst of simultaneous requests across workers.",
    )
    parser.add_argument(
        "--retry-pass-delay", type=int, default=60,
        help="Seconds to wait before the serial retry pass over whatever the "
             "parallel pass could not clone.",
    )
    parser.add_argument(
        "--clone-attempts", type=int, default=5,
        help="Attempts per dist-git clone before giving up. A clone that fails "
             "because the package does not exist is not retried.",
    )
    parser.add_argument(
        "--clone-cooldown", type=float, default=COOLDOWN_BASE, metavar="SECONDS",
        help="First pause taken by every worker once src.fedoraproject.org "
             "starts refusing clones; it doubles per wave. 0 disables the "
             "wait, which is only useful against a stub server.",
    )
    args = parser.parse_args()

    if args.packages:
        wanted = [(name, args.dest / name) for name in args.packages]
    elif args.build_order:
        wanted = build_order_packages(args.build_order, args.tiers)
    elif args.catalog:
        wanted = catalog_packages(args.catalog)
    else:
        parser.error("pass a catalog, --build-order or --package")

    state: dict[str, dict] = {}
    if args.state and args.state.exists():
        state = json.loads(args.state.read_text())

    imported = skipped = failed = 0
    with tempfile.TemporaryDirectory(prefix="tunaos-distgit-") as temp:
        tempdir = pathlib.Path(temp)
        pending = []
        for package, relative in wanted:
            target = relative if relative.is_absolute() else pathlib.Path.cwd() / relative
            if target.exists():
                print(f"Skipping {package}: {target} already exists")
                skipped += 1
                continue
            pending.append((package, relative, target))

        throttle = Throttle(base=args.clone_cooldown)
        pacer = Pacer(interval=args.clone_pace)
        jobs = calibrate_jobs(len(pending), args.jobs)

        def clone_one(item):
            package, _, _ = item
            return item, clone_with_retry(
                package, args.branch, tempdir / package, args.clone_attempts,
                throttle=throttle,
                pacer=pacer,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            outcomes = list(pool.map(clone_one, pending))

        # Second pass, serial, after a cooldown.
        #
        # The step exits 1 on any failure and `Build tiers` is then skipped, so
        # the whole run turns on the worst clone of the batch. Run 31271496131
        # imported 258 of 263 and lost the run over the other five -- after 76
        # retries had already recovered from "the remote end hung up".
        #
        # Per-clone retry cannot fix that on its own: at even a 2% residual
        # failure rate, a 263-package import almost never comes out clean, and
        # the full manifest is 1248. What is left after the parallel pass is a
        # handful of packages against a host that has been shedding load, so
        # the useful move is to stop competing with ourselves -- wait, then go
        # one at a time. Serial and patient is what a rate-limited server is
        # asking for.
        failed_first = [item for item, clone in outcomes if clone.returncode != 0
                        and not clone_is_permanent_failure(clone.stderr or "")]
        if failed_first:
            print(f"{len(failed_first)} clone(s) still failing; "
                  f"cooling down {args.retry_pass_delay}s then retrying serially")
            time.sleep(args.retry_pass_delay)
            retried = {
                item[0]: clone_with_retry(
                    item[0], args.branch, tempdir / item[0], args.clone_attempts
                )
                for item in failed_first
            }
            outcomes = [(item, retried.get(item[0], clone))
                        for item, clone in outcomes]

        for (package, relative, target), clone in outcomes:
            checkout = tempdir / package
            if clone.returncode != 0:
                tail = clone.stderr.strip().splitlines()[-1:] or ["clone failed"]
                print(f"FAILED {package}: {tail[0]}")
                failed += 1
                continue
            commit = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            target.mkdir(parents=True)
            for source in checkout.iterdir():
                if source.name == ".git":
                    continue
                if source.is_dir():
                    shutil.copytree(source, target / source.name)
                else:
                    shutil.copy2(source, target / source.name)
            spec = bump_release(target) if args.release_bump else None
            state[package] = {
                "branch": args.branch,
                "commit": commit,
                "path": str(relative),
                "release_bumped": bool(spec),
            }
            print(f"Imported {package} from {args.branch} at {commit[:12]}")
            imported += 1

    if args.state:
        args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    print(f"imported={imported} skipped={skipped} failed={failed}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
