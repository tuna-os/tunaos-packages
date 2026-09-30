#!/usr/bin/env python3
"""
COPR Build Chain Engine

Triggers COPR builds tier-by-tier from build-order.yml.
Packages within a tier are submitted in parallel; tiers are sequential.

Bootstrap handling:
  - When spec_override is set, the COPR package name is derived from the
    spec filename (e.g. glib2-bootstrap.spec → COPR package glib2-bootstrap).
    This avoids overwriting the production package definition.
  - When no spec_override, the existing COPR package definition is used as-is;
    only build-package is triggered (no definition edits).
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml


def log(msg):
    print(f"==> {msg}", flush=True)


def err(msg):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)


def run(cmd, check=True, capture=True):
    result = subprocess.run(cmd, capture_output=capture, text=True, check=False)
    if check and result.returncode != 0:
        err(f"Command failed: {' '.join(cmd)}\n{result.stderr.strip()}")
        return None
    if capture:
        return result.stdout.strip()
    return "" if result.returncode == 0 else None


def get_rpm_name(spec_file):
    """Return the RPM Name: field from a spec file."""
    try:
        res = run(["rpmspec", "-q", "--qf", "%{name}\n", spec_file], check=False)
    except FileNotFoundError:
        res = None
    if res:
        return res.splitlines()[0]
    with open(spec_file, errors="ignore") as f:
        for line in f:
            if line.lower().startswith("name:"):
                return line.split(":", 1)[1].strip()
    return None


def resolve_pkg(pkg_path, spec_override, copr_name_override=None):
    """
    Return (copr_pkg_name, spec_file, rpm_name).

    If copr_name_override is set:
      - copr_pkg_name = copr_name_override (no local spec needed)
      - spec_file = None, rpm_name = copr_name_override
      - used for distgit packages already registered in COPR by known name
    If spec_override is set:
      - copr_pkg_name = spec filename without .spec  (e.g. glib2-bootstrap)
      - this avoids clobbering the production COPR package definition
    Else:
      - copr_pkg_name = RPM Name from the spec
    """
    if copr_name_override:
        return copr_name_override, None, copr_name_override

    if spec_override:
        spec_file = os.path.join(pkg_path, spec_override)
        copr_pkg_name = Path(spec_override).stem
    else:
        specs = [s for s in Path(pkg_path).glob("*.spec")
                 if "bootstrap" not in s.name and "rawhide" not in s.name]
        if not specs:
            specs = list(Path(pkg_path).glob("*.spec"))
        if not specs:
            return None, None, None
        spec_file = str(specs[0])
        copr_pkg_name = None  # will be filled from RPM name

    if not os.path.exists(spec_file):
        err(f"Spec not found: {spec_file}")
        return None, None, None

    rpm_name = get_rpm_name(spec_file)
    if copr_pkg_name is None:
        copr_pkg_name = rpm_name

    return copr_pkg_name, spec_file, rpm_name


def ensure_copr_package(project, pkg_name, pkg_path, spec_file):
    """
    Register or update a COPR SCM package definition.
    Only called for bootstrap packages (spec_override); regular packages
    are assumed to already be defined correctly.
    """
    repo_root = run(["git", "rev-parse", "--show-toplevel"])
    remote_url = run(["git", "remote", "get-url", "origin"])
    if remote_url and remote_url.startswith("git@github.com:"):
        remote_url = remote_url.replace("git@github.com:", "https://github.com/")
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    subdir = os.path.relpath(pkg_path, repo_root)
    spec_filename = os.path.basename(spec_file)

    log(f"  Registering COPR package: {pkg_name} (spec={spec_filename})")
    base_args = [
        "copr-cli", "__VERB__", project,
        "--name", pkg_name,
        "--clone-url", remote_url,
        "--commit", branch,
        "--subdir", subdir,
        "--spec", spec_filename,
        "--method", "make_srpm",
    ]

    add = list(base_args)
    add[1] = "add-package-scm"
    res = run(add, check=False)
    if res is None:
        edit = list(base_args)
        edit[1] = "edit-package-scm"
        if run(edit, check=True) is None:
            return False
    return True


def trigger_build(project, pkg_name, chroot, dry_run=False):
    """Submit a build for one package/chroot. Returns build ID or None."""
    if dry_run:
        log(f"  [dry-run] build-package {pkg_name} --chroot {chroot}")
        return "dry-run"

    log(f"  Triggering {pkg_name} on {chroot}...")
    out = run([
        "copr-cli", "build-package", project,
        "--name", pkg_name,
        "--chroot", chroot,
        "--nowait",
    ], check=False, capture=True)

    if out is None:
        err(f"  Failed to submit build for {pkg_name} on {chroot}")
        return None

    # Extract build ID from output like: "Created builds: 10226936"
    m = re.search(r"Created builds:\s*(\d+)", out)
    if not m:
        m = re.search(r"/build/(\d{6,})", out)
    if m:
        build_id = m.group(1)
        log(f"  Submitted build {build_id} for {pkg_name} on {chroot}")
        return build_id
    log(f"  Submitted (no ID parsed) for {pkg_name} on {chroot}")
    return "unknown"


def get_build_state(build_id):
    """Return current state string for a build ID, or None on error."""
    out = run(["copr-cli", "status", build_id], check=False)
    if out:
        return out.strip()
    # Fallback: parse list-builds (expensive but reliable)
    return None


def wait_for_builds(build_ids, project, poll_interval=30):
    """
    Poll until all submitted build IDs reach a terminal state.
    Returns dict of {build_id: state}.
    """
    if not build_ids or all(b in ("dry-run", "unknown") for b in build_ids):
        return {}

    terminal = {"succeeded", "failed", "cancelled", "skipped"}
    states = {}
    pending = [b for b in build_ids if b not in ("dry-run", "unknown", None)]

    log(f"  Waiting for {len(pending)} builds to finish...")
    while pending:
        # Use list-builds to get recent statuses
        out = run(["copr-cli", "list-builds", project], check=False)
        if out:
            for line in out.splitlines():
                # Format: ID\tPackage\tState
                parts = line.split("\t")
                if len(parts) >= 3:
                    bid, _pkg, state = parts[0].strip(), parts[1].strip(), parts[2].strip()
                    if bid in pending and state in terminal:
                        states[bid] = state
                        pending.remove(bid)
                        log(f"  Build {bid} ({_pkg}): {state}")

        if not pending:
            break
        log(f"  {len(pending)} builds still pending... sleeping {poll_interval}s")
        time.sleep(poll_interval)

    return states


def main():
    parser = argparse.ArgumentParser(description="COPR Build Chain Engine")
    parser.add_argument("--manifest", default="build-order.yml")
    parser.add_argument("--project", default="jreilly1821/c10s-gnome-50")
    parser.add_argument("--chroot", action="append", dest="chroots",
                        default=[], help="Chroot(s) to build for (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-wait", action="store_true",
                        help="Submit all builds without waiting between tiers")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="Continue to next tier even if a build fails")
    parser.add_argument("--tier", help="Only build a specific tier")
    parser.add_argument("--from-tier", help="Skip all tiers before this one (resume mid-chain)")
    args = parser.parse_args()

    if not args.chroots:
        args.chroots = ["epel-10-x86_64"]

    if not os.path.exists(args.manifest):
        err(f"Manifest not found: {args.manifest}")
        sys.exit(1)

    with open(args.manifest) as f:
        manifest = yaml.safe_load(f)

    log(f"Project:  {args.project}")
    log(f"Chroots:  {', '.join(args.chroots)}")
    log(f"Manifest: {args.manifest}")
    log(f"Wait:     {not args.no_wait}")

    all_failed = []
    skipping = bool(args.from_tier)

    for tier in manifest["tiers"]:
        tier_name = tier["name"]
        if skipping:
            if tier_name == args.from_tier:
                skipping = False
            else:
                log(f"Skipping tier: {tier_name}")
                continue
        if args.tier and tier_name != args.tier:
            continue

        log("")
        log(f"===== Tier: {tier_name} =====")

        tier_build_ids = []
        tier_pkg_names = []

        for pkg in tier.get("packages", []):
            pkg_path = pkg.get("path", "")
            spec_override = pkg.get("spec_override")
            copr_name_override = pkg.get("copr_name")
            is_bootstrap = bool(spec_override)

            copr_name, spec_file, rpm_name = resolve_pkg(
                pkg_path, spec_override, copr_name_override
            )
            if not copr_name:
                err(f"Cannot resolve spec for {pkg_path}, skipping")
                continue

            spec_label = os.path.basename(spec_file) if spec_file else "(distgit)"
            log(f"  Package: {copr_name} (rpm={rpm_name}, spec={spec_label})")

            # For bootstrap packages, ensure the COPR definition exists/is updated
            if is_bootstrap and not args.dry_run:
                if not ensure_copr_package(args.project, copr_name, pkg_path, spec_file):
                    err(f"Failed to register {copr_name}, skipping")
                    continue

            for chroot in args.chroots:
                bid = trigger_build(args.project, copr_name, chroot, dry_run=args.dry_run)
                if bid:
                    tier_build_ids.append(bid)
            tier_pkg_names.append(copr_name)

        if not args.no_wait and not args.dry_run and tier_build_ids:
            # Give COPR a moment to register the builds
            time.sleep(10)
            states = wait_for_builds(tier_build_ids, args.project)

            failed = [bid for bid, s in states.items() if s != "succeeded"]
            if failed:
                err(f"Tier {tier_name}: {len(failed)} builds failed: {failed}")
                all_failed.extend(failed)
                if not args.continue_on_error:
                    err("Stopping. Use --continue-on-error to proceed anyway.")
                    sys.exit(1)
            else:
                log(f"Tier {tier_name}: all builds succeeded.")

    if all_failed:
        err(f"Finished with failures: {all_failed}")
        sys.exit(1)
    else:
        log("Build chain complete.")


if __name__ == "__main__":
    main()
