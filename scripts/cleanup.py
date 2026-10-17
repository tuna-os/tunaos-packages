#!/usr/bin/env python3
"""
RPM Repository Cleanup Script

Keeps only the latest N versions of each package to save storage costs.
Can be run as part of CI/CD or as a standalone cron job.
"""

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def run_cmd(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def parse_nevr(filename: str) -> tuple[str, str, str, str]:
    """Parse NEVR (Name-Epoch-Version-Release) from RPM filename."""
    match = re.match(r"^(.+)-(\d+):(.+)-(.+)\.rpm$", filename)
    if match:
        name, epoch, version, release = match.groups()
        return name, epoch, version, release
    return filename, "0", "", ""


def get_rpm_version_sort_key(filename: str) -> tuple:
    """Create a sort key for RPM versions."""
    name, epoch, version, release = parse_nevr(filename)

    # Try to parse version/release into sortable components
    def parse_ver(v: str):
        parts = []
        for c in v:
            if c.isdigit():
                if parts and parts[-1].isdigit():
                    parts[-1] += c
                else:
                    parts.append(c)
            else:
                parts.append(c)
        return [int(x) if x.isdigit() else x for x in parts]

    return (name, int(epoch), parse_ver(version), parse_ver(release))


def cleanup_bucket(bucket: str, max_versions: int = 3, dry_run: bool = True):
    """Clean up old RPM versions from R2 bucket."""
    print(f"Scanning bucket: {bucket}")
    print(f"Keeping {max_versions} versions per package")
    print(f"Dry run: {dry_run}")
    print("-" * 50)

    # Get list of all objects
    cmd = ["aws", "s3", "ls", f"s3://{bucket}/repo/", "--recursive"]
    output = run_cmd(cmd)

    # Group by package name
    packages = defaultdict(list)

    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 4:
            continue

        size = parts[2]
        path = parts[3]

        if path.endswith(".rpm"):
            filename = os.path.basename(path)
            packages[filename].append(
                {
                    "path": path,
                    "size": size,
                    "filename": filename,
                }
            )

    # Sort each package's versions and mark old ones for deletion
    to_delete = []
    total_saved = 0

    for filename, versions in packages.items():
        if len(versions) <= max_versions:
            continue

        # Sort by version (newest first)
        sorted_versions = sorted(
            versions,
            key=lambda x: get_rpm_version_sort_key(x["filename"]),
            reverse=True,
        )

        # Mark old versions for deletion
        for old_version in sorted_versions[max_versions:]:
            to_delete.append(old_version["path"])
            total_saved += int(old_version["size"])

    print(f"Found {len(to_delete)} old packages to remove")
    print(f"Would save: {total_saved / 1024 / 1024:.2f} MB")

    if dry_run:
        print("\nPackages that would be deleted:")
        for path in to_delete:
            print(f"  {path}")
    else:
        print("\nDeleting old packages...")
        for path in to_delete:
            print(f"  Deleting: {path}")
            run_cmd(["aws", "s3", "rm", f"s3://{bucket}/{path}"])

        # Update repodata after deletion
        print("\nUpdating repository metadata...")
        # This would typically be done via the main build workflow

    print("-" * 50)
    print("Cleanup complete!")


def cleanup_local(directory: str, max_versions: int = 3, dry_run: bool = True):
    """Clean up old RPM versions from local directory."""
    print(f"Scanning directory: {directory}")
    print(f"Keeping {max_versions} versions per package")
    print(f"Dry run: {dry_run}")
    print("-" * 50)

    rpm_files = list(Path(directory).glob("**/*.rpm"))

    packages = defaultdict(list)
    for rpm_path in rpm_files:
        filename = rpm_path.name
        packages[filename].append(
            {
                "path": rpm_path,
                "size": rpm_path.stat().st_size,
                "filename": filename,
            }
        )

    to_delete = []
    total_saved = 0

    for filename, versions in packages.items():
        if len(versions) <= max_versions:
            continue

        sorted_versions = sorted(
            versions,
            key=lambda x: get_rpm_version_sort_key(x["filename"]),
            reverse=True,
        )

        for old_version in sorted_versions[max_versions:]:
            to_delete.append(old_version["path"])
            total_saved += int(old_version["size"])

    print(f"Found {len(to_delete)} old packages to remove")
    print(f"Would save: {total_saved / 1024 / 1024:.2f} MB")

    if dry_run:
        print("\nPackages that would be deleted:")
        for path in to_delete:
            print(f"  {path}")
    else:
        print("\nDeleting old packages...")
        for path in to_delete:
            print(f"  Deleting: {path}")
            path.unlink()

    print("-" * 50)
    print("Cleanup complete!")


def main():
    parser = argparse.ArgumentParser(description="Clean up old RPM versions")
    parser.add_argument("--bucket", help="R2/S3 bucket name")
    parser.add_argument("--directory", "-d", help="Local directory to clean")
    parser.add_argument(
        "--keep",
        "-k",
        type=int,
        default=3,
        help="Number of versions to keep (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be deleted without deleting",
    )

    args = parser.parse_args()

    if args.bucket:
        cleanup_bucket(args.bucket, args.keep, args.dry_run)
    elif args.directory:
        cleanup_local(args.directory, args.keep, args.dry_run)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
