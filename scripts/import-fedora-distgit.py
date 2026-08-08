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
import time
import tempfile

import yaml

RELEASE = re.compile(r"^(Release:\s*)(\d+)(%\{\?dist\}.*)$", re.MULTILINE)



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
# The host also returns 503 under load, and we are part of that load -- the
# workflow clones with --jobs 8, all against one server. Run 31268302766 with
# retries on:
#
#   Retrying python-wheel (1/2): ... The requested URL returned error: 503
#   Retrying python-editables (2/2): ... The requested URL returned error: 503
#   imported=8 skipped=0 failed=2
#
# Eight of eleven clones needed a retry and six of them recovered, so retrying
# is right; three attempts over six seconds is just too impatient for a server
# that is asking us to slow down. Hence five attempts, backing off to 16s.
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
    package, branch, checkout, attempts=3, runner=None, sleeper=None, jitter=None
):
    runner = runner or subprocess.run
    sleeper = sleeper or time.sleep
    url = f"https://src.fedoraproject.org/rpms/{package}.git"
    result = None
    for attempt in range(1, max(1, attempts) + 1):
        # git refuses to clone into an existing non-empty directory, so a
        # partial checkout left by a failed attempt would turn one transient
        # error into a permanent one.
        if checkout.exists():
            shutil.rmtree(checkout, ignore_errors=True)
        result = runner(
            ["git", "clone", "--depth", "1", "--branch", branch, url, str(checkout)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result
        if clone_is_permanent_failure(result.stderr or ""):
            return result
        if attempt < max(1, attempts):
            tail = (result.stderr or "").strip().splitlines()[-1:] or ["clone failed"]
            print(f"Retrying {package} ({attempt}/{attempts - 1}): {tail[0]}")
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
        "--jobs", type=int, default=4,
        help="Parallel dist-git clones. The clones are network-bound and "
             "independent; the copy and the state file stay serial so the "
             "result does not depend on completion order. They all hit one "
             "host, though, so this is also how hard src.fedoraproject.org "
             "is being pushed -- see --clone-attempts.",
    )
    parser.add_argument(
        "--clone-attempts", type=int, default=5,
        help="Attempts per dist-git clone before giving up. A clone that fails "
             "because the package does not exist is not retried.",
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

        def clone_one(item):
            package, _, _ = item
            return item, clone_with_retry(
                package, args.branch, tempdir / package, args.clone_attempts
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            outcomes = list(pool.map(clone_one, pending))

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
