# CAS Garbage Collection & Reachability (Report-Only)

Tideforge Content-Addressed Storage (CAS) stores package build artifacts as
immutable blobs indexed by ActionResults and repository metadata. Over time,
superseded build waves, unreferenced candidate packages, and test artifacts
accumulate in storage.

The mark-and-sweep garbage collection subsystem (#430 step 5, #485) measures
reachability, applies an age grace period, and records tombstones without
deleting any live objects.

## Storage Contract

Production CAS paths in R2:

```text
actions/sha256/<action-key>.json     -- Authoritative ActionResult manifests
blobs/sha256/<artifact-digest>       -- Immutable content blobs
```

Protected `main` publishers write blobs first and the ActionResult last.

## Mark Phase

The reachability engine traverses two categories of retained roots:

1. **Retained ActionResults**: All active ActionResult manifests under
   `actions/sha256/*.json`. Each artifact entry (`name`, `size`, `digest`)
   marks its content blob as reachable.
2. **Current Repository Metadata**: All package entries in served indexes
   declared by `manifests/package-factory.yaml` (`published_index` for RPM,
   DEB, and Arch). Any blob matching a served package digest or path is
   marked as reachable.

The union of these two sets forms the complete **reachable blob set**.

## Sweep Phase (Report-Only)

Any CAS blob not present in the reachable set is identified as **unreachable**.

### Age Grace Period

To protect in-flight builds (where blobs are uploaded before the ActionResult
manifest is published) and recent build waves:

- Default grace period: **7 days** (604,800 seconds).
- Configurable via `--grace-period-days` or `--grace-period-seconds`.
- Blobs younger than the grace period are classified as `grace` (protected).
- Blobs older than or equal to the grace period are classified as `candidate`
  (eligible for tombstoning).

### Last-Referencing ActionResult Attribution

When historical ActionResults are available, unreachable blobs are matched to
their last referencing ActionResult. This attributes why the blob was created
(e.g., a superseded wave of `glib2-2.87.3-1.el10.x86_64.rpm`). Blobs never
referenced by any ActionResult are flagged as orphans.

### Tombstone Manifest Format

Eligible sweep candidates past the grace period receive a formal tombstone
record (Schema 1):

```json
{
  "schema": 1,
  "tombstone": {
    "digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "size": 1048576,
    "path": "blobs/sha256/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "created_at": "2026-08-20T10:00:00+00:00",
    "unreachable_since": "2026-08-22T00:00:00+00:00",
    "tombstone_time": "2026-08-30T00:00:00+00:00",
    "grace_period_seconds": 604800,
    "last_referencing_action": "sha256:deadbeef...",
    "last_referencing_artifact": "glib2-2.87.3-1.el10.x86_64.rpm",
    "last_referencing_time": "2026-08-20T10:05:00+00:00",
    "reason": "unreachable_past_grace_period"
  }
}
```

## Running the GC Report

Run the reachability check locally or against storage listings:

```bash
# Scan a local CAS directory
python3 scripts/cas_gc.py --cas-dir /path/to/cas --grace-period-days 7 --summary

# Generate JSON and Markdown reports from an inventory listing
python3 scripts/cas_gc.py --inventory-lsf inventory.txt \
  --json cas-gc-report.json \
  --markdown cas-gc-report.md \
  --tombstones-dir tombstones/
```

## Safety Policy

1. **Strictly Report-Only**: No delete operations exist in `scripts/cas_gc.py`.
2. **Destructive Sweep Gating**: Destructive deletion is deferred to a future
   phase after reachability reports run clean across production waves.
