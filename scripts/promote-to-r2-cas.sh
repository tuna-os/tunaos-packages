#!/usr/bin/env bash
# Promote validated package artifacts and ActionResult to authoritative R2 CAS.
#
# Contract (#430 step 4 / #484):
#   1. Acquire or check renewable lease (leases/sha256/<key>.json).
#   2. Verify local ActionResult against artifact directory.
#   3. Upload/sync all content blobs first (blobs/sha256/<digest>).
#   4. Upload/sync the ActionResult last (actions/sha256/<key>.json) as atomic commit marker.
#   5. Release lease upon completion.
set -euo pipefail

CELL_ID=""
ACTION_KEY=""
BUCKET="${R2_BUCKET:-bluefin}"
STAGE_DIR=".cas-stage"
HOLDER="${GITHUB_RUN_ID:-$$}"
TTL=2400

while [ $# -gt 0 ]; do
  case "$1" in
    --cell-id)    CELL_ID="$2"; shift 2 ;;
    --action-key) ACTION_KEY="$2"; shift 2 ;;
    --bucket)     BUCKET="$2"; shift 2 ;;
    --stage-dir)  STAGE_DIR="$2"; shift 2 ;;
    --holder)     HOLDER="$2"; shift 2 ;;
    --ttl)        TTL="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$CELL_ID" ] || [ -z "$ACTION_KEY" ]; then
  echo "usage: $0 --cell-id ID --action-key KEY [--bucket NAME]" >&2
  exit 2
fi

RESULT_FILE=".factory/${CELL_ID}/action-result.json"
ARTIFACT_DIR=".factory/${CELL_ID}/artifacts"

if [ ! -f "$RESULT_FILE" ]; then
  echo "ERROR: ActionResult not found at ${RESULT_FILE}" >&2
  exit 1
fi
if [ ! -d "$ARTIFACT_DIR" ]; then
  echo "ERROR: Artifact directory not found at ${ARTIFACT_DIR}" >&2
  exit 1
fi

mkdir -p "$STAGE_DIR"

# Stage blobs and action result locally into CAS structure
python3 scripts/tideforge-action-cache.py promote \
  --result "$RESULT_FILE" \
  --artifact-dir "$ARTIFACT_DIR" \
  --cas-dir "$STAGE_DIR" \
  --holder "$HOLDER" \
  --expected-action-key "$ACTION_KEY"

# If rclone is available and configured for R2, upload blobs first, ActionResult last
if command -v rclone >/dev/null 2>&1 && [ -f "${HOME}/.config/rclone/rclone.conf" ]; then
  echo "==> Uploading CAS blobs to r2:${BUCKET}/blobs/..."
  rclone copy "${STAGE_DIR}/blobs" "r2:${BUCKET}/blobs"
  echo "==> Uploading ActionResult commit marker to r2:${BUCKET}/actions/..."
  rclone copy "${STAGE_DIR}/actions" "r2:${BUCKET}/actions"
  echo "==> Successfully promoted ${ACTION_KEY} to authoritative R2 CAS"
else
  echo "==> Local CAS staging complete at ${STAGE_DIR}"
fi
