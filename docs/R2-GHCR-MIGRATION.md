# R2 to GHCR: distribution migration (#912)

## Scope and evidence

Inventory as of 2026-10-19. The goal is to remove the R2 dependency for publication, as [utah-packages](https://github.com/projectbluefin/utah-packages) does.
We must preserve installed systems and accumulated packages.
The original stage is a **manual, isolated publication rehearsal**, not the
completed migration. The ORAS work below also adds a disabled-by-default
production branch. No live objects, credentials, family tags, Worker routes
or consumer pins have changed.

### Publishers

| Path | Current behavior | Migration disposition |
| --- | --- | --- |
| `.github/workflows/publish-build-chain-rpms.yml` | R2 sync-down, sign/index accumulated RPMs, R2 sync-up, signed GHCR family image, HTTP verification | First candidate lane; preserve the shared `publish-rpms` lock |
| `.github/workflows/publish-tideforge-rpms.yml` | RPM target paths and mirrors on R2 | Separate follow-on with target/mirror coverage |
| `.github/workflows/publish-tideforge-debs.yml` | Debian/Ubuntu signed APT repositories on R2 | Keep live; APT is not an OCI client |
| `.github/workflows/publish-tideforge-arch.yml` | Arch package databases and packages on R2 | Keep live; Pacman is not an OCI client |
| `.github/workflows/publish-tideforge-gentoo.yml` | Gentoo binary repository and overlay on R2 | Keep live; separate format and consumer validation |
| `.github/workflows/build.yml`, `build-distributed.yml` | Legacy build/publication paths also reference R2 | Audit triggers and actual usage before retirement |
| `.github/workflows/package-factory-cell.yml`, `cas-gc.yml` | Build/action cache and CAS operations | Outside this distribution migration; do not disable/delete |
| `.github/workflows/pages.yml`, `r2-inventory.yml` | Catalog/inventory generation | Read paths also need inventory; Pages is not a substitute RPM origin |

Utah's `.github/workflows/publish-repository.yml` verifies and materializes
a prior OCI repository before adding the next wave. Its `/repository`
layout and digest-pinned consumption are the useful model. A catalog page
does not itself serve a usable DNF/APT/Pacman repository.

### Consumers and routes that prevent an immediate R2 shutdown

- Installed-system definitions and bootstrap scripts: `contrib/tuna-os.repo`,
  `contrib/install.sh`, `contrib/install-gnome49.sh`; including URLs for the public GPG key.
- Mock buildroots: `mock/*.cfg` use `repo.tunaos.org` family and target URLs.
  Some also materialize Utah repositories on disk. These do not replace all HTTP repos.
- Factory measurements/plans: `manifests/package-factory.yaml`,
  `scripts/list-served-nvrs.py`, `scripts/verify-published-wave.py` and callers
  depend on live indexes and package reachability.
- `workers/repo-proxy.ts` routes existing URLs to R2. The `/repo/...`
  transform and direct family paths are distinct. This change preserves those routes.
- tuna-os/tunaOS image builds already have digest-pinned GHCR inputs and
  `/repository` bind mounts, but installed-system HTTP access remains a
  separate contract. Audit each image's enabled repositories before cutover.

## Rehearse publication without R2

After maintainers review and merge this workflow, manually run
`publish-oci-candidate.yml` on main with:

1. One planner-recognized build-chain `cell`, such as `gnome50-el10-x86_64`.
2. The `sha256:...` digest now behind that cell's live GHCR family tag.
3. A completed main-branch `publish-build-chain-rpms.yml` `wave_run_id` with
   a retained `publish-rpm-<cell>` artifact. Partial-failure builds may have useful RPMs.
   The validator rejects cancelled, unfinished, unrelated and fork runs.

The lock prevents updates to the live tag while the candidate validates it. The requested digest must equal the selected cell's
current tag, and must verify under the exact main build-chain publisher's
keyless identity. Registry pull or verification failures stop the job;
there is no empty seed, alternate registry, R2 fallback, or tag substitution.

The seed must carry nonempty architecture-matching RPMs and GPG-verifiable
repodata. The workflow materializes the seed first. It then calls the shared script for RPM signatures,
reverse dependencies, deduplication, the no-shrink guard, and repomd signatures.
Foreign-package eviction is intentionally not enabled for this rehearsal.

The output is a new, signed
`<cell>-candidate-<run-id>-<attempt>` tag and immutable digest. No production
tag advances. The run uploads signature evidence, input run metadata,
digest and signed repomd. A failed push/signature step is not a promoted or
qualified repository. Candidate tags have a different signer workflow;
production trust policies must not silently accept them as live indexes.

### Cost measurement boundaries

The candidate publication job makes **no calls to the R2 API**: no R2 secrets,
rclone configuration, sync-down, sync-up or HTTP origin verification.
The old path enumerates/downloads an accumulated family and uploads/syncs
its next state on every publish. Avoided request counts and bytes depend
on the family tree and rclone listing/comparison behavior; do not claim a
bill reduction from YAML alone. The source wave may have been built and
published using R2 before the rehearsal, and mock/build/cache consumers
still use it. Record actual R2 operation/byte telemetry for comparable
waves before you claim cost savings. The draft does not access invoices.

## Promotion and retirement checklist

- [ ] Execute the candidate for each intended family/architecture. Compare
  package NEVRA/checksum inventory with the seed and the staged wave. Explain
  every intentional supersession; old package names must not disappear.
- [ ] Pull the **candidate digest** on a fresh runner. Verify the exact signer identity and GPG repodata. Check package checksums.
  Do a representative DNF dependency resolution/install with
  `gpgcheck=1` and `repo_gpgcheck=1`, including the actual image install set.
- [ ] Prove candidate publication works with no R2 credentials. Archive
  successful run evidence and measure data/request deltas.
- [ ] Design serialized live OCI advancement with verified previous-digest
  carry-forward and stale-base rejection. Add a safe initial bootstrap and
  rollback procedure. A manual stale digest must never replace newer work.
- [ ] Move image/build consumers to verified digest pins, with architecture
  mapping and complete install/boot qualification. Preserve a known-good pin.
- [ ] Separate the live HTTP compatibility publisher from the authoritative
  OCI pipeline. Preserve incremental/additive packages, metadata signatures,
  and no-shrink guards. Verify actual existing URLs on installed systems.
- [ ] Choose and validate an HTTP distribution solution for DNF/APT/Pacman
  and Gentoo before ending their R2 updates. OCI registries alone do not
  provide their repository protocols. Get approval for any route changes.
- [ ] Audit legacy writers, GPG keys, catalog assets, buildroots, scheduled
  jobs and non-image consumers. Treat cache/CAS costs independently.
- [ ] Only after qualified consumer migration and a rollback window, retire
  redundant publication to R2. Object deletion and credential changes need
  separate approval and are not part of this change.

Do not close #912 on the strength of this first-stage rehearsal alone.

## ORAS transport and the Utah distribution model

Utah main `96856f43e3bbef8e9e40e8e26c5fab654e71e3c0` publishes a signed
scratch OCI image with Podman. Its ORAS source-lookaside RFC is a proposed
source contract, not an implemented package publisher. We reuse its coherent
native repository, immutable image digest and local native-client model;
we use ORAS for transport, not as a replacement package manager.

The candidate workflow uses official ORAS **1.3.4**, with committed release
archive hashes for both runner architectures. `scripts/oras_distribution.py`
creates an OCI image layout with standard image/config/layer media types and
`/repository` content. `oras cp --from-oci-layout` copies this image to the
isolated candidate tag. A generic `oras push` artifact would not supply the
root filesystem required by existing `COPY --from` consumers and is not used.
The manifest digest is computed locally before upload; signing and round-trip
verification use that digest, not a resolution of the mutable tag.

The pull command requires a GHCR digest and an exact Cosign workflow identity.
It verifies the signer before ORAS downloads the layout. It verifies manifest,
config and layer bytes, target/format/architecture scope, and safe archive
members before an atomic rename exposes the repository. It does not execute
package content. Failure leaves no destination repository and has no HTTP,
empty-tree, alternate-registry or R2 fallback. Existing unsigned or unscoped
OCI images are not accepted by this new consumer: the rehearsal still imports
its old seed through the existing verified Podman path.

Transport preserves complete RPM (including openSUSE), DEB, Arch and Gentoo
repository trees and signature files. Safe file aliases such as Arch database
symlinks become identical regular files. Native package metadata retains its
original bytes. This transport does **not** replace native GPG validation,
repository indexing, dependency-closure checks, per-target admission, or
native-client install tests. The RPM candidate keeps the existing shared
wave merger and its reverse-dependency/signature guards, then verifies the
pulled tree against the original and verifies its repomd signature again.

The format/target/architecture labels describe the publication scope, not a
claim that packages have passed native validation. Callers must use the
existing format-specific planner and signing/solver gates before transport.
The isolated RPM candidate and an explicitly admitted build-chain RPM branch
are wired locally. No workflow has been dispatched and no live publisher,
consumer pin, tag, HTTP route or credential has changed. APT and Pacman remain
native clients: either materialize this verified tree into a local repository
or provide a separately approved compatible HTTP origin before their cutover.

### Verification boundaries

Local fixture tests cover every format and both architectures, deterministic
manifest identities, actual ORAS layout-to-layout copy, byte preservation,
corrupt/missing identities, scope mismatches, unsafe archive members, failed
signatures and registry errors. Fixtures are not real signed native packages.
A live GHCR publication, real Cosign/OIDC round trip, native dependency solving,
package installation, existing COPY consumers and installed-system upgrade
paths still require execution of the controlled rehearsal and qualification
checklist above. No production migration is complete on the basis of these tests.

Official transport and installation references:
[ORAS copy](https://oras.land/docs/commands/oras_cp/) and
[ORAS installation](https://oras.land/docs/installation/).


## Opt-in build-chain production branch (not activated)

`publish-build-chain-rpms.yml` keeps `distribution: r2` as its default. The
new `oras` branch requires all of these reviewed operator choices:

- `ORAS_PRODUCTION_ENABLED=true` and a JSON `ORAS_PRODUCTION_CELLS` list in
  repository variables. These variables are not set by this code change.
- An explicit `oras_seeds` JSON map with exactly the selected cell IDs and
  their current immutable manifest digests. No empty or tag bootstrap exists.
- Main-branch execution, no dry-run promotion, existing target admission,
  and the shared `publish-rpms` workflow lock. The new planner rejects legacy
  R2 publication for admitted cells so an old R2 tree cannot overwrite them.

The new publication job receives no R2 credentials, never calls R2, and does
not use HTTP verification as a substitute for verifying its actual output.
It verifies the existing signed cell digest before Podman imports that old
image. This deliberate bootstrap path accepts the existing OCI format before
scope labels exist. It retains and verifies the accumulated repository,
checks wave architecture/dist, and invokes the existing RPM wave merger.
There is no foreign-package eviction in this lane.

The native verifier uses fresh GPG and RPM trust databases populated only
from committed `public.gpg`. It requires actual RPM signatures, not merely
successful digest checks. It verifies both the seed and the final pulled
repository, so a mismatched secret signing key cannot produce an accepted
publication even if that key is already present on the runner.

The publisher uploads a run-unique staging tag, signs the local immutable
digest, verifies and materializes that digest, compares all bytes, verifies
native signatures again, and rechecks the previous live digest. Only then
may `oras tag` update the cell channel. The immutable prior digest is retained
in a receipt for rollback. An intent record precedes the tag command and is
uploaded even on failure; a timeout or failed post-write read is **uncertain**,
not proof the tag stayed unchanged. Inspect the current digest before any retry.

### Single-writer activation prerequisite

An OCI tag PUT is atomic for one manifest, but is **not compare-and-swap**.
The second read is only safe against cooperating writers under the shared
lock. Before activation, operators must restrict/disable other manual,
external, old-revision and legacy writers to admitted cell tags. All such
writers must honor the same lock. Repository-variable admission in the new
workflow cannot constrain old revisions or unrelated registry credentials.
Do not activate until this is enforced and a staged failure rehearsal passes.
Rollback is a separately authorized move to the retained, verified prior
digest under the same lock, after confirming the current channel state.

The branch changes publication only. Its preceding build job still uses
existing buildroot/cache/source paths; therefore an ORAS production run is
not proof that the full factory no longer depends on R2. Tideforge RPM,
DEB/APT, Arch/Pacman and Gentoo publishers are not switched by this branch.
They need the same explicit seed/admission/signature/solver contracts rather
than reuse of an RPM-specific merger. The transport already preserves those
formats, but transport fixtures do not qualify their production pipelines.

### Capacity and runtime

The scoped image uses one uncompressed layer; this favors transparent native
repository bytes and straightforward verification, not minimum download size.
A 32 GiB repository needs about 32 GiB for its current tree and approximately
65 GiB additional scratch capacity for the downloaded layer plus its extracted
round trip. The publisher checks for `2 * repository_bytes + 1 GiB` spare before
upload. Archive/member overhead and concurrent disk users can still exhaust
space; failures stop before channel advancement. Production has a 180-minute
job limit; bandwidth and full-family runtime have not been measured live.

The archive is streamed once directly to the selected layout filesystem;
there is no second complete temporary tar. Upload layout bytes are removed
before pull. `/mnt` is preferred for image/layout scratch where available.
Verification bounds metadata blobs to 4 MiB, members to one million, one file
to 64 GiB and aggregate extracted bytes to the verified raw layer size;
sparse files, links and special members are rejected on extraction. Safe
source file aliases are normalized to ordinary files before publication.

## Minimal no-R2 consumer path and compatibility decisions

Core tunaOS documents image updates through `uupd`/`bootc`, and application
installation through Flatpak, Homebrew and containers. Those users do not need
a new native HTTP package service merely because the build repository moved.
The minimal core path is to materialize verified ORAS/OCI repositories during
image builds, run native package managers against local repositories with all
signature and dependency checks intact, then remove/disable temporary configs
after the final native transaction. Day-2 image updates remain GHCR-based.

Utah implements this pattern: its Containerfile binds the repository for the
build and disables the temporary repo after the last install. See
[Utah's final repository cleanup](https://github.com/projectbluefin/utah/blob/17222de27c6db6c0a32eb704c72814ff8f7b644d/Containerfile#L346-L366).
A size-changing disable comment avoids losing the intended change in a layer
diff; cleanup must be verified in the final image, not inferred from a command.

The separate compatibility boundary is explicit standalone native use:
`contrib/install.sh`, `contrib/install-gnome49.sh` and `contrib/tuna-os.repo`
promise DNF installation/updates over `repo.tunaos.org`. Those interfaces cannot
be silently broken or labeled build-only. Operators must choose either a
non-R2 HTTP-compatible origin for those clients or a documented, approved
retirement/migration of that interface. Neither choice is made by this code.
Existing R2 endpoints stay untouched until that decision and consumer rollout.
