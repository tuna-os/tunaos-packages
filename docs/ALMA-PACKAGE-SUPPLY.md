# Alma package supply

`alma10` and `alma10-kitten` build separate packages for Albacore and
Yellowfin. Intel uses the pinned `linux/amd64/v2` child; ARM uses its native
`linux/arm64` child. Factory RPMs use `x86_64` or `aarch64`. Alma vendor
dependencies may use `x86_64_v2`; that label alone proves no CPU compatibility.

The target contract selects each image and publication prefix. Generic
packages publish under `rpm/<target>/<arch>`. Native GNOME and XFCE families
use `<family>/<target>-<arch>`. Existing CentOS Stream paths stay independent.

## Evidence and bootstrap

Recipe builds retain installed buildroot inventory, native repositories,
dependency transactions, compiler versions, effective flags, and source
digests. Compiler policy preserves native hardening and applies the v2 or
ARMv8 baseline after recipe environment exports. Conflicting CPU overrides
fail the build.

Native Mock chains begin with signed native repositories and an empty local
candidate repository. Each run generates an ephemeral signing key outside
artifacts. RPM signatures are verified before insertion. Metadata and its
signature are staged and verified before replacement under the repository
lock. Public keys and candidate scope accompany buildroot observations;
private keys are removed when the chain exits.

These keys establish local candidate integrity. They confer no production
signing authority. Previously served NVRs, restored RPMs and cached roots
cannot substitute for an Alma rebuild. Published sources remain disabled
until an authenticated immutable snapshot supplies matching evidence.

CPU inspection binds exact RPM bytes, ELF machine/ISA requirements and
linked-library providers. Missing static requirements need artifact-bound
restricted CPU execution. Vendor inputs need authenticated signing,
repository and baseline evidence rather than invented compiler flags.
Inspection always leaves `readiness` false: the publication gate must verify
producer trust and the clean consumer transaction before promotion.

## Complete factory selection

Selections larger than the three existing matrix shards are partitioned
into deterministic batches. Native chains retain reserved continuation
slots. Every selected cell appears exactly once in the original inventory;
no package is dropped to fit a matrix.

The parent dispatches remaining batches with the exact source commit and
selection digest. It retains child run identities and requires matching
successful results from every batch and attempt. Missing, pending or failed
children leave coverage blocked. Batch completion is separate from package
publication readiness.

## Diagnosed failures

**SYMPTOM:** A genuine v2 build fails on `-mtls-dialect=gnu2`.
**CAUSE:** The compiler guard treated every positive `-m` option as an ISA
extension, including this TLS ABI selector from Alma's native macros.
**FIX:** Allow the exact GNU2 selector while rejecting unknown TLS options
and CPU extensions. Compiler policy tests include the measured native flags.

**SYMPTOM:** A candidate signature parser accepts a bad signature alongside
a successful payload digest.
**CAUSE:** Independent substring checks matched `Signature` and a different
line's `OK`.
**FIX:** Require successful signature status on the same line. Candidate
repository tests preserve this corruption regression.

**SYMPTOM:** A repository symlink redirects bootstrap writes.
**CAUSE:** Resolving the destination before checking its components hides
the symlink.
**FIX:** Reject symlinks before resolution; keep private candidate state
outside the repository and retain atomic replacement tests.
