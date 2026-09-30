# Tideforge action cache

Tideforge identifies a package build by canonical declared inputs rather than a
workflow run. The key covers the complete recipe directory, selected target and
architecture, the selected target's contract and dependency-capability slice,
the immutable OCI build-image digest, the package-format renderer set, dependency
action keys, and the reproducibility contract including SOURCE_DATE_EPOCH.

Only renderer inputs used by a target are hashed: a Debian assembler change does
not invalidate RPM, Arch, or openSUSE actions. Every build image is resolved to
an image@sha256 digest before key generation.

An ActionResult records the exact action key plus every artifact's safe basename,
byte size, and SHA-256. Restore verifies the requested key, schema, unique safe
filenames, sizes, and hashes before a result may skip compilation. Lint,
clean-install, and smoke validation still run on hits.

GitHub Actions cache is an acceleration transport. Workflows restore before
compilation and save only after all validation; the cache action never saves
implicitly. R2 uses `actions/sha256/<action-key>.json` as the authoritative
result index, `blobs/sha256/<artifact-digest>` for immutable content, and
`leases/sha256/<action-key>.json` for renewable action-key publication leases.
Protected main jobs alone may publish trusted R2 results, writing blobs first
and the ActionResult last.

## Authoritative CAS promotion and lease contract

1. **CAS storage layout**:
   - `actions/sha256/<action-key>.json`: Immutable ActionResult metadata (atomic commit marker).
   - `blobs/sha256/<artifact-digest>`: Content-addressed immutable artifact bytes.
   - `leases/sha256/<action-key>.json`: Ephemeral renewable action-key publication lease.

2. **Renewable action-key leases**:
   - Workers acquire 30–45 minute renewable leases before starting builds or promotions (`scripts/tideforge-action-cache.py acquire-lease`).
   - A worker that loses or fails to renew its lease cannot promote to authoritative CAS.
   - Waiters check for valid ActionResults upon lease expiration.

3. **Atomic commit ordering**:
   - All referenced content blobs are verified and synchronized first (`blobs/sha256/<digest>`).
   - The authoritative ActionResult (`actions/sha256/<action-key>.json`) is written strictly last as the commit marker.
   - An ActionResult in CAS guarantees every referenced blob is already present and digest-verified.

4. **Promotion behind the factory boundary**:
   - Publication is a thin promotion step over CAS: read verified ActionResults, verify digests, and copy exact bytes into served repository indexes.
   - Builds on protected default branch publish authoritative ActionResults and blobs directly to R2.
