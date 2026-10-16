# Alma package supply

`alma10` and `alma10-kitten` build separate packages for Albacore and
Yellowfin. Intel uses the pinned `linux/amd64/v2` child; ARM uses its native
`linux/arm64` child. Factory RPMs use `x86_64` or `aarch64`. Alma vendor
dependencies may use `x86_64_v2`; that label alone proves no CPU compatibility.

The contract for each target selects its image and publication prefix.
Generic packages publish under `rpm/<target>/<arch>`. For GNOME and XFCE,
native families use `<family>/<target>-<arch>`. Paths for CentOS Stream stay
independent.

## Evidence and bootstrap

For each recipe, the build keeps an inventory of packages in its buildroot.
It also keeps repositories, dependency transactions, compiler versions,
effective flags, and source digests. The policy for compilers preserves
native security flags. It applies the v2 or ARMv8 baseline after the recipe
exports its environment. CPU overrides that conflict with this policy fail
the build.

For native chains, Mock uses repositories with signatures and an empty
repository for local candidates. Each run creates a temporary key to sign
candidates. Keep that key outside artifacts. The helper checks each RPM
signature before it adds the RPM. It stages metadata with its signature and
checks both before replacement. Hold the repository lock for these changes.
Keep the public key and candidate scope with observations of the buildroot.
The chain removes private keys on exit.

These keys establish integrity for local candidates. They give no authority
to sign production supply. NVRs from prior publication, restored RPMs and
cached roots cannot substitute for an Alma rebuild. Keep published sources
disabled until an authenticated immutable snapshot gives the same evidence.

For CPU inspection, bind evidence to exact RPM bytes and ELF requirements
for machine and ISA. Bind each linked library to its provider. If static
requirements are absent, execute the exact artifact on a restricted CPU.
For vendor inputs, authenticate signatures and evidence for the repository
and baseline. Do not invent compiler flags for those inputs. Inspection
always leaves `readiness` false. Before promotion, the publication gate must
verify the producer's authority and a clean transaction for the consumer.

## Complete factory selection

The planner splits selections into deterministic batches if they exceed
the three existing matrix shards. Keep slots for continuation of native
chains. Include each selected cell once in the original inventory. Do not
drop packages to fit a matrix.

The parent dispatches the other batches with the exact source commit and
selection digest. Keep the identity of each child run. Each batch and try
needs a successful result with the same identity. Coverage stays blocked
if a child is absent, incomplete or failed. Batch completion does not
establish readiness for package publication.

## Diagnosed failures

**SYMPTOM:** A genuine v2 build fails on `-mtls-dialect=gnu2`.
**CAUSE:** The guard for compilers treated every positive `-m` option as an
ISA extension. That included the TLS ABI selector from Alma's native macros.
**FIX:** Allow the exact GNU2 selector. Reject unknown TLS options and CPU
extensions. Tests for the compiler policy include the measured native flags.

**SYMPTOM:** The parser for candidate signatures accepts a bad signature
beside a successful payload digest.
**CAUSE:** Separate text checks matched `Signature` and a different line's
`OK`.
**FIX:** The signature must report success on the same line. Tests for the
candidate repository preserve this corruption regression.

**SYMPTOM:** A symlink in the repository redirects writes during bootstrap.
**CAUSE:** The path resolver hid the symlink before it checked the components.
**FIX:** Reject symlinks before resolution. Keep private state for candidates
outside the repository. Keep tests for atomic replacement.
