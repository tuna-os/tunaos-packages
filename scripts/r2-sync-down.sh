#!/usr/bin/env bash
# Sync a served repository prefix down from R2 before a publish rewrites it.
#
#   r2-sync-down.sh --src REMOTE --dest DIR [--min-files N] [--pattern GLOB]
#                   [-- RCLONE_ARGS...]
#
#   --src        the rclone remote path, e.g. "r2:${R2_BUCKET}/tideforge/arch/"
#   --dest       the local tree the publish will later sync back up
#   --min-files  refuse to continue if fewer than N files matching --pattern
#                came down (default 0: no floor, the first-publish case)
#   --pattern    find -name glob the floor counts (default '*')
#   after --     extra rclone arguments, e.g. --exclude "repodata/**"
#
# Every publisher here does sync-down -> sign/index -> sync-up, and the
# sync-up is `rclone sync`, which makes the bucket MATCH the local tree. So
# anything that fails to come down here is DELETED from the bucket there.
# That is #124 / INCIDENT-repo-wipe-gnome, and #519: repo/10/x86_64 lost
# ~160 served package names between 2026-08-20 and 2026-08-25 because a
# partial sync-down was swallowed. publish-rpm-wave.sh's NEVER SHRINK guard
# cannot catch it: it counts files in the local tree, and the lost files
# never arrived to be counted. The guard has to live here, at the sync-down.
#
# The rules:
#
#   EXIT 3          rclone's "directory not found": nothing is published at
#                   this prefix yet, so there is nothing to delete. The first
#                   publish to a new prefix continues.
#   ANY OTHER FAIL  stops the job before the destructive sync-up, not after.
#   FLOOR           a prefix known to hold many packages refuses to continue
#                   from a suspiciously small download. A prefix the
#                   publisher creates passes 0, because any floor would make
#                   its first publish impossible. The caller's planner decides
#                   which is which (plan-rpm-publish.py's min_rpms).
set -euo pipefail

src="" dest="" min_files=0 pattern='*'
rclone_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --src)       src="$2";       shift 2 ;;
    --dest)      dest="$2";      shift 2 ;;
    --min-files) min_files="$2"; shift 2 ;;
    --pattern)   pattern="$2";   shift 2 ;;
    --)          shift; rclone_args=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
: "${src:?--src is required}"
: "${dest:?--dest is required}"
case "$min_files" in
  ''|*[!0-9]*) echo "--min-files must be a non-negative integer, got '$min_files'" >&2; exit 2 ;;
esac

mkdir -p "$dest"
set +e
rclone sync "$src" "$dest" "${rclone_args[@]+"${rclone_args[@]}"}"
rc=$?
set -e
if ((rc == 3)); then
  echo "::notice::no existing objects at ${src} — treating as first publish"
elif ((rc != 0)); then
  echo "::error::sync-down of ${src} failed (rclone rc=${rc}); refusing to continue — the sync-up in this job would DELETE every object that failed to come down"
  exit 1
fi

count=$(find "$dest" -type f -name "$pattern" | wc -l)
echo "synced down ${count} file(s) matching ${pattern} from ${src}"
if ((min_files > 0 && count < min_files)); then
  echo "::error::only ${count} file(s) matching ${pattern} synced down from ${src}; expected at least ${min_files}; refusing to risk a repo wipe"
  exit 1
fi
