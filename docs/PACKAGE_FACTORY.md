# TunaOS Package Factory

This repository is the source-controlled package factory for TunaOS. It builds
and signs packages in GitHub Actions, tests them against declared distro
targets, and publishes only validated repositories to Cloudflare R2.

## Supported targets

| Target | Format | Repository | Status |
|---|---|---|---|
| EL10 | RPM | rpm-md | supported |
| Ubuntu | DEB | APT | supported foundation |
| Debian Sid | DEB | APT | supported foundation |
| openSUSE Tumbleweed | RPM | rpm-md | supported foundation |
| Arch | pkg.tar.zst | pacman | scaffold |

The authoritative target and R2-path contract is
[`manifests/package-factory.yaml`](https://github.com/tuna-os/tunaos-packages/blob/main/manifests/package-factory.yaml).

## One contract, one build engine per target

Targets share the contract, but engines differ.
A package counts only when it builds in **its target root**, installs from the staged repository on **that target**, and starts a session.
We never repackage binaries built for one target for another target.
The build root is an ABI input.
Binary reuse across roots leads to ABI failures (`docs/experiments/tideforge-universal-intermediate.md`, and `docs/HUMMINGBIRD-TARGET.md` §2).

| Target | Engine | Root | Runtime gate |
|---|---|---|---|
| EL10 | mock + native specs (`src/gnome-5x`, `src/deps`, `src/xfce-wayland`) | `centos-stream-10-ci` (+CRB, EPEL as build inputs) | `build-gnome50-verify.yml` (Lima VM, GDM) |
| Fedora | mock + native specs | `fedora-44-ci` | clean-install cell |
| Hummingbird | mock + Rawhide dist-git imports | Fedora 44 + public-hummingbird by priority (`hummingbird-ci*.cfg`) | static installability walk + `dnf --assumeno` inside the pinned bootc-os image |
| Ubuntu, Debian | Tideforge → `debian/` + container build | the target's own container | clean-install cell; session smokes declared, not yet implemented |
| openSUSE Tumbleweed | Tideforge → spec + zypper build | Tumbleweed container | clean-install cell |
| Arch | Tideforge/native PKGBUILD + makepkg | archlinux container | clean-install cell |

What every engine shares -- and what "one contract" means:

- **the roots**: a desktop manifest lists what a tunaOS image installs; the gap engine checks the target index;
- **the gates**: install from the staged repository on the target, run the installability walk, and test the target image;
- **the status board** (`docs/FACTORY-STATUS.md`): built-vs-needed per target and architecture, from live indexes, so a target that publishes nothing says so;
- **the promotion rule** below.

Two components are not built here.
The system consumes **GNOME on Hummingbird** from `projectbluefin/utah-packages`, pinned by digest on the hummingbird target as a `consumed_indexes` entry.
TunaOS counts whatever it ships as already had (`docs/HUMMINGBIRD-TARGET.md` §7-8).
**tromso and xfce-linux** (KDE and XFCE on freedesktop-sdk, BuildStream) are whole-OS images built in their own repositories.
They are products in tunaOS, not package sources for targets here.

We declare a target only with a build leg, a served index, and a consumer.
All declared architectures except for `arch` aarch64 meet that standard.
openSUSE meets the first two and waits on the third.

## Asking for a desktop on a target

The deliverable is a stack, not a repository.
`scripts/request.py` is the front door.
It measures distance to a desktop across ordered stages: important packages first, then image packages, then the tail:

```
just want "gnome 51 on hummingbird"              # what it would build
just want-measured "gnome 51 on hummingbird"     # distance to a working stack
```

Order matters more than count.
On 2026-08-28, the hummingbird index served 580/673 of the build order and 3/10 of the packages a GNOME session needs.
A convergence tool that reports the first number wastes waves while `gdm` and `gnome-shell` are absent.

TunaOS checks whether the image boots.
While the contract stage remains open, an image build cannot boot into a session.

For the bringup loop on a host that remembers between tries, see [WARM-BUILDER.md](WARM-BUILDER.md).
For the design, see [RFC 012](rfc/rfc012-request-driven-convergence.md).

## Upstream source policy

Bluefin, Aurora, and Fedora dist-git are inputs for source and spec metadata only.
Before you import a package, record its upstream commit, license, patches, and target compatibility.
TunaOS rebuilds the package itself.
It never enables an upstream binary repository in a produced image.

Maintainers track the status of parity and the order of delivery in [`UPSTREAM_PARITY.md`](UPSTREAM_PARITY.md).

## Promotion contract

Every candidate must build in the target buildroot, pass package tests, install from the staged repository, and pass runtime tests.
Only then may CI sign and promote it to the stable R2 path.
ORAS is suitable for immutable source bundles, not as the
live DNF/APT/Pacman endpoint.

The process publishes the promoted index as an OCI image (`ghcr.io/tuna-os/tunaos-packages:gnome50-el10-x86_64`), where `/repository` holds the signed RPMs and repodata.
This image is the build input: tunaOS pins the digest in `image-versions.yaml` and bind-mounts `/repository` as a `file://` repository for installation.
The system consumes packages from [repo.tunaos.org](https://repo.tunaos.org) (maintainer directive 2026-09-03: no more COPR; #673).

### What enforces this today

The unified factory from RFC 011 (`docs/rfc/rfc011-unified-gap-driven-factory.md`) enforces this contract in two workflows:

- **`package-factory.yml`** is the single planner and required gate. It computes affected coordinates `(package | native family, target, architecture, release track, engine)` from changed paths.
- **`package-factory-cell.yml`** is the reusable build boundary for each cell. It derives an action key from exact inputs, restores cached results, and otherwise builds fresh. It builds in clean roots, lints artifacts, stages them in a temporary repository, and runs clean installs before smoke checks.

Two engines create payloads inside this boundary.
`tideforge` cells build recipes from `packages/<name>/package.yaml`.
`build-chain` cells build native spec families (`src/gnome-5x`, `src/deps`, `src/xfce-wayland`) from tiered manifests (`build-order*.yml`).
See `manifests/package-builds.yaml`.

Because a desktop family is large, the factory converges across runs:

- The nightly schedule runs hummingbird desktop cells at 12:00 UTC.
- A weekly schedule runs each `build-chain` family on Sunday at 03:00 UTC.
- A cell that reaches the 6-hour limit uploads its partial artifacts. The next run restores them and continues.
- The drift workflow (`gap-drift.yml`) re-measures declared gaps against live indexes and opens review PRs.

`scripts/factory-status.py` measures built versus needed packages per target into `docs/FACTORY-STATUS.md`.
It diffs each run against the last merged measurement and renders deltas and days without movement.
It flags regressed packages and alerts when the refresh fails to run.

The tooling publishes that measurement as a GitHub Pages site (`scripts/render-factory-site.py`).
The site renders `docs/factory-status.json`, `manifests/package-factory.yaml`, and `manifests/package-builds.yaml`.
It republishes when source data changes.

The same site provides a package browser for [repo.tunaos.org](https://repo.tunaos.org).
The script `scripts/snapshot-repo-contents.py` reads every declared `published_index` through `scripts/repo_index.py`.
If the tool cannot read an index, it lists the index with the reason.

### Preventive checks

The build loop uses four checks adapted from sandogasa (`docs/SANDOGASA-ADAPTATIONS.md`).
The script `scripts/repo_index.py` provides format-neutral index inspection for RPM, DEB, and Pacman.
`scripts/preflight-buildrequires.py` checks build and install satisfiability before dispatch.
`scripts/check-published-hygiene.py` audits served prefixes for duplicate entries and conflicts.
Publish workflows stop updates that leave dependencies broken (`check-reverse-deps.py`, `check-index-regression.py`).
Build chains save manifests of buildroots so failed runs diff against successful ones (`scripts/diff-buildroots.py`).

### What the gate does not cover

Recorded here on purpose. A gate whose exceptions are implicit reads as full coverage to the next person, which is the exact failure this section exists to prevent.

| Not covered | Scope | Why |
| --- | --- | --- |
| Runtime/session gates | all recipes | The 12 gate types the target-queue manifests declare (`greetd-login`, `*-session-smoke`, `selinux-enforcing`, …) are not implemented — see `TIDEFORGE-READINESS.md` and RFC 011 Phase 3. The install + smoke check above is the deepest automated gate today. |
| Staged install against the full desktop closure | `build-chain` families | The clean-install verify resolves from the target's system repositories, the published factory index, and the cell's own artifacts. A root package whose runtime closure is not yet fully published can pass build + lint while its desktop cannot yet be assembled — `docs/FACTORY-STATUS.md` is the honest ledger of that distance. |

The full plan includes every recipe for every declared target.
The catalog tests (`tests/test_catalog_completeness.py`) verify that every executed package has a catalog entry and payload.

**Publication to R2 is a manual step.**
Maintainers stopped the automated promotion after the repository incident (`INCIDENT-repo-wipe-gnome.md`).
Publishers (`publish-tideforge-rpms.yml`, `publish-tideforge-debs.yml`, `publish-tideforge-arch.yml`, `publish-build-chain-rpms.yml`) run only via `workflow_dispatch`.
A maintainer triggers each wave, and a verify job checks the served index before completion.

When we reintroduce automated promotion, it must depend on the factory gate.
Two rules preserve the stability:

- Do not add path-filtered jobs as required checks in branch protection (#128).
- Do not gate on conclusions that include skipped jobs.

## Package layout

New work should use this shape:

```text
packages/<name>/
  source.yaml             # upstream URL, revision, license, checksum
  rpm/<target>/*.spec     # RPM packaging and patches
  debian/                 # Debian packaging
  arch/PKGBUILD           # Arch scaffold when supported
  opensuse/*.spec          # openSUSE scaffold when supported
```

Maintainers migrate existing `src/` packages incrementally.
They remain build inputs until moved without changes to published NVRs.

## Target-native overlays

Shared source graphs do not imply shared metadata.
The GNOME queue in `manifests/target-queues/gnome.yaml` keeps EL10 bootstrap and SELinux overlays native to RPM, while Debian and Ubuntu build DEB packages.

## Tideforge: single-recipe workflow

Developers work on Tideforge in parallel with native pipelines.
The native pipelines remain the production path until Tideforge matches build, install, and runtime parity.

Use `packages/_template/package.yaml` as the author recipe.
`scripts/tideforge.py` validates recipes, shows build plans, and renders RPM or Debian package files:

```bash
python3 scripts/tideforge.py validate packages/my-package/package.yaml
python3 scripts/tideforge.py plan packages/my-package/package.yaml --target el10
python3 scripts/tideforge.py render packages/my-package/package.yaml --target ubuntu --output out/ubuntu
```

Before you add a native dependency name to the catalog, probe the target container with `scripts/probe-target-dependencies.py`.
This resolves recipe capabilities (for example `dbus-dev`) to native package names and checks live repository metadata without installs:

```bash
python3 scripts/probe-target-dependencies.py packages/my-package/package.yaml --dry-run
python3 scripts/probe-target-dependencies.py packages/my-package/package.yaml --target el10
python3 scripts/probe-target-dependencies.py packages/my-package/package.yaml --json
```

The tool creates the files for native package tools, but authors edit a single recipe.
A target override applies only to dependency or build differences that cannot be made portable.

When an upstream archive has no git submodules, use the optional list of `sources` instead of a git clone in a build command.
Each auxiliary archive has an HTTPS URL, SHA-256, filename, destination below the primary source tree, and optional `strip_components`.
Tideforge renders those archives as native RPM/Pacman sources and extracts them before the build.
This keeps a source closure reviewable and reproducible.
A recipe is not eligible for promotion until its target CI builds the complete closure.

Cargo recipes build with `--locked` by default.
An upstream release with a stale root entry in `Cargo.lock` may set `build.cargo_locked: false`.
It must include a specific `build.cargo_lock_reason`.
Maintainers accept it only after review of the lockfile diff in the target build.
