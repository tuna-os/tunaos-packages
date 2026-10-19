# R2 to GHCR: distribution migration (#912)

## Scope and evidence

Inventory as of 2026-10-19. The goal is to remove the recurring R2
publication dependency, following [utah-packages](https://github.com/projectbluefin/utah-packages),
without breaking installed systems or dropping accumulated packages.
This first stage is a **manual, isolated publication rehearsal**, not the
completed cost migration. It makes no changes to existing publishers,
objects, credentials, live family tags, Worker routes, or consumer pins.

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
does not itself serve a working DNF/APT/Pacman repository.

### Consumers and routes that prevent an immediate R2 shutdown

- Installed-system definitions and bootstrap scripts: `contrib/tuna-os.repo`,
  `contrib/install.sh`, `contrib/install-gnome49.sh`; including public GPG key URLs.
- Mock buildroots: `mock/*.cfg` use `repo.tunaos.org` family and target URLs.
  Some use local materialized Utah repositories as well, not instead of all HTTP repos.
- Factory measurements/plans: `manifests/package-factory.yaml`,
  `scripts/list-served-nvrs.py`, `scripts/verify-published-wave.py` and callers
  depend on live indexes and package reachability.
- `workers/repo-proxy.ts` routes existing URLs to R2. The `/repo/...`
  transform and direct family paths are distinct. No routing changes here.
- tuna-os/tunaOS image builds already have digest-pinned GHCR inputs and
  `/repository` bind mounts, but installed-system HTTP access remains a
  separate contract. Audit each image's enabled repositories before cutover.

## Rehearse publication without R2

After this workflow is reviewed and merged, manually run
`publish-oci-candidate.yml` on main with:

1. One planner-recognized build-chain `cell`, such as `gnome50-el10-x86_64`.
2. The `sha256:...` digest currently behind that cell's live GHCR family tag.
3. A completed main-branch `publish-build-chain-rpms.yml` `wave_run_id` with
   a retained `publish-rpm-<cell>` artifact. Partial-failure builds may have
   useful RPMs; cancelled/in-progress/unrelated/fork runs are rejected.

The lock prevents another live publisher advancing the tag while the
candidate validates it. The requested digest must equal the selected cell's
current tag, and must verify under the exact main build-chain publisher's
keyless identity. Registry pull or verification failures stop the job;
there is no empty seed, alternate registry, R2 fallback, or tag substitution.
The seed must carry nonempty architecture-matching RPMs and GPG-verifiable
repodata. It is materialized before invoking the existing shared signing,
reverse-dependency, deduplication, never-shrink, and repomd-signature logic.
Foreign-package eviction is intentionally not enabled for this rehearsal.

The output is a new, signed
`<cell>-candidate-<run-id>-<attempt>` tag and immutable digest. No production
tag advances. The run uploads signature evidence, input run metadata,
digest and signed repomd. A failed push/signature step is not a promoted or
qualified repository. Candidate tags have a different signer workflow;
production trust policies must not silently accept them as live indexes.

### Cost measurement boundaries

The candidate publication job makes **zero R2 API calls**: no R2 secrets,
rclone configuration, sync-down, sync-up or HTTP origin verification.
The old path enumerates/downloads an accumulated family and uploads/syncs
its next state on every publish. Avoided request counts and bytes depend
on the family tree and rclone listing/comparison behavior; do not claim a
bill reduction from YAML alone. The source wave may have been built and
published using R2 before the rehearsal, and mock/build/cache consumers
still use it. Record actual R2 operation/byte telemetry for comparable
waves before claiming savings. The draft does not access billing data.

## Promotion and retirement checklist

- [ ] Execute the candidate for each intended family/architecture. Compare
  package NEVRA/checksum inventory with the seed plus staged wave. Explain
  every intentional supersession; old package names must not disappear.
- [ ] Pull the **candidate digest** on a fresh runner, verify its exact
  candidate signer identity and GPG repodata, and check package checksums.
  Perform a representative DNF dependency resolution/install with
  `gpgcheck=1` and `repo_gpgcheck=1`, including the actual image install set.
- [ ] Prove candidate publication works with no R2 credentials. Archive
  successful run evidence and measure data/request deltas.
- [ ] Design serialized live OCI advancement with verified previous-digest
  carry-forward and stale-base rejection. Add a safe initial bootstrap and
  rollback procedure. A manual stale digest must never replace newer work.
- [ ] Move image/build consumers to verified digest pins, with architecture
  mapping and complete install/boot qualification. Preserve a known-good pin.
- [ ] Separate the live HTTP compatibility publisher from the authoritative
  OCI pipeline, preserving incremental/additive packages, metadata signatures,
  and no-shrink guards. Verify actual existing URLs on installed systems.
- [ ] Choose and validate an HTTP distribution solution for DNF/APT/Pacman
  and Gentoo before ending their R2 updates. OCI registries alone do not
  provide their repository protocols. Obtain approval for any routing cutover.
- [ ] Audit legacy writers, GPG keys, catalog assets, buildroots, scheduled
  jobs and non-image consumers. Treat cache/CAS costs independently.
- [ ] Only after qualified consumer migration and a rollback window, retire
  redundant R2 publication. Object deletion and credential changes require
  separate approval and are not part of this change.

Do not close #912 on the strength of this first-stage rehearsal alone.
