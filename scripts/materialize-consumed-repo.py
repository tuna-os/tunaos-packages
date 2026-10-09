#!/usr/bin/env python3
"""Materialise a consumed OCI rpm-md repository for a mock/dnf buildroot.

projectbluefin/utah-packages publishes its Hummingbird GNOME stack only as
an OCI image (`FROM scratch; COPY repository /repository`) -- there is no
HTTP baseurl for mock to read. This pulls the pinned digest, streams the
layer(s), and writes the repository tree to --out so the mock config's
`[utah]` file:// repo has something to point at.

Fail-closed: any fetch error, a missing arch manifest, or a missing
repository/ tree is an error, never an empty repo -- an empty buildroot
repo silently changes every resolution in the chain. The single exception
is --allow-empty, used for arches utah does not publish (aarch64 today):
it writes a valid but package-less repodata so dnf falls through to the
other repos exactly as it does today.

Idempotent: when --out/.provenance.json names the same ref and arch, the
tree is left alone (the chain-band workflow additionally caches --out by
digest, so most shards never reach the registry at all).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import oci_repository as oci

EMPTY_REPOMD = """<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo" xmlns:rpm="http://linux.duke.edu/metadata/rpm">
<revision>0</revision>
<data type="primary"><checksum type="sha256">{sha}</checksum><location href="repodata/primary.xml.gz"/><timestamp>0</timestamp><size>0</size></data>
</repomd>
"""
EMPTY_PRIMARY = """<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="0">
</metadata>
"""


def fail(message: str) -> int:
    print(f"materialize-consumed-repo: ERROR: {message}", flush=True)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", required=True, help="oci://host/path@sha256:<digest>")
    parser.add_argument("--arch", default="x86_64", help="rpm arch (utah publishes x86_64)")
    parser.add_argument("--out", type=pathlib.Path, required=True,
                        help="directory receiving repository/ (mock bind-mounts it at /run/utah-repo)")
    parser.add_argument("--cache-dir", type=pathlib.Path, default=pathlib.Path(".cache/oci"))
    parser.add_argument("--allow-empty", action="store_true",
                        help="write a valid empty repo when the arch has no manifest")
    args = parser.parse_args(argv)

    try:
        host, path, digest = oci.parse_ref(args.ref)
    except SystemExit as exc:
        return fail(str(exc))

    out = args.out
    prov_file = out / ".provenance.json"
    if prov_file.exists():
        try:
            prior = json.loads(prov_file.read_text())
            if prior.get("ref") == args.ref and prior.get("arch") == args.arch:
                print(f"materialize-consumed-repo: cached {args.ref} for {args.arch} "
                      f"({prior.get('files', 0)} files)")
                return 0
        except (json.JSONDecodeError, OSError):
            pass

    token = oci.pull_token(host, path)
    try:
        manifest = oci.manifest_for(host, path, digest, args.arch, token)
    except SystemExit as exc:
        if args.allow_empty:
            return write_empty(out, args, reason=str(exc))
        return fail(str(exc))

    repo_dir = out / "repository"
    try:
        stats = oci.extract_tree(host, path, manifest, token, repo_dir)
    except Exception as exc:  # noqa: BLE001 - network/tar failures must fail the step
        return fail(f"extracting {args.ref}: {exc}")
    if stats["files"] == 0:
        return fail(f"{args.ref}: no repository/ members in any layer")
    if stats["skipped"]:
        print(f"materialize-consumed-repo: skipped {len(stats['skipped'])} "
              f"unsafe members: {stats['skipped'][:5]}", flush=True)

    primary = repo_dir / "repodata" / "repomd.xml"
    primary_sha = (hashlib.sha256(primary.read_bytes()).hexdigest()
                   if primary.exists() else None)
    prov_file.write_text(json.dumps({
        "ref": args.ref, "arch": args.arch, "files": stats["files"],
        "layers": stats["layers"], "repomd_sha256": primary_sha,
    }, indent=2, sort_keys=True) + "\n")
    print(f"materialize-consumed-repo: {stats['files']} files from {args.ref} "
          f"into {repo_dir}")
    return 0


def write_empty(out: pathlib.Path, args: argparse.Namespace, reason: str) -> int:
    import gzip

    repo_dir = out / "repository" / "repodata"
    repo_dir.mkdir(parents=True, exist_ok=True)
    blob = gzip.compress(EMPTY_PRIMARY.encode())
    (repo_dir / "primary.xml.gz").write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()
    (repo_dir / "repomd.xml").write_text(
        EMPTY_REPOMD.format(sha=sha).replace("<size>0</size>",
                                             f"<size>{len(blob)}</size>"))
    (out / ".provenance.json").write_text(json.dumps({
        "ref": args.ref, "arch": args.arch, "files": 0, "empty": True,
        "reason": reason,
    }, indent=2, sort_keys=True) + "\n")
    print(f"materialize-consumed-repo: empty repo for {args.arch} ({reason})",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
