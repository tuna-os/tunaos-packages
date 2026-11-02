# ADR 0002: Gentoo as a factory target — rendered overlay plus signed gpkg binhost

- Status: accepted
- Date: 2026-10-16
- Issue: [#838](https://github.com/tuna-os/tunaos-packages/issues/838)
- Sign-off: hanthor (maintainer) delegated approval for the Tuna Desktop
  v1.0.0-beta.1 rollout (tuna-desktop ADR 0008)
- Policy: tunaOS RFC lifecycle (ADR 0004). A new package format and
  publish route changes package supply. We record the decision here.

## Context

guppy (tunaOS Containerfile.gentoo) is a Gentoo image that consumes only
`::gentoo` and Gentoo's official binhost. To ship tuna-desktop to it, the
factory needs a Gentoo package path. Before this, the factory had none: no
gentoo target in `manifests/package-factory.yaml`, no renderer in
`scripts/tideforge.py`, no gpkg branch in the cell runner, and no publisher.
Every tuna-desktop dependency is already stable on amd64 in `::gentoo`
(gtk4-layer-shell 1.3.0, gtk 4.20, libadwaita 1.8.7, Rust 1.98.1).

## Decision

1. **The recipe stays the source of truth.** The command `tideforge.py
   render --target gentoo` writes a one-package overlay (`profiles/repo_name` =
   `tunaos`, `masters = gentoo`, thin Manifests). It outputs an EAPI 8 ebuild
   at `<outputs.gpkg.category>/<name>/<name>-<PV>.ebuild`. The recipe version
   maps to PMS (`1.0.0~beta.1` → `1.0.0_beta1`). SPDX licenses map to Gentoo
   names. The recipe custom build and install commands run unchanged. A recipe
   with the `rust` capability inherits `cargo.eclass`. Its `RUST_DEPEND` maps
   to `build.rust_version`, and the build runs under `cargo_env`. Sources use
   pinned archives renamed to `${P}-<filename>` to prevent collision in
   `DISTDIR`. The renderer writes the Manifest from verified sha256 bytes.
2. **One `gentoo` target, format `gpkg`.** The cell emerges the ebuild
   inside the desktop/systemd stage3 (the profile guppy uses). It takes
   dependencies from `::gentoo` and Gentoo's signed binhost. Then it saves the
   gpkg. Verification installs that gpkg into a fresh stage3 without an overlay.
   Thus, the package arrives only as the binary under test.
3. **Publish a signed binhost and overlay.** The workflow
   `publish-tideforge-gentoo.yml` signs each gpkg with the factory key
   (`gpkg-sign`). It regenerates `Packages` with `emaint binhost --fix`. It
   publishes the binhost to `https://repo.tunaos.org/gentoo/binpkgs/<arch>/`.
   The publisher serves the Gentoo overlay as a zip file at
   `https://repo.tunaos.org/gentoo/overlay/tunaos-overlay.zip`. Verification
   trusts the factory key, needs signatures on binary packages, and installs
   from the served URL.

**Considered options:**

- **A hand-written ebuild in a separate overlay repository**: rejected. It
  duplicates recipe sources, commands, and layout. The Gentoo build would
  drift from the other eight targets.
- **Ship only an overlay and let guppy compile**: rejected. guppy builds
  from binaries because `@world` from source exceeds the limit for CI time. A
  build of tuna-desktop from source in every image repeats that cost.
- **Rendered overlay with signed binhost**: chosen.

## Consequences

- The ebuild renderer supports `build_system: custom` only. Other build
  systems fail to prevent untested ebuilds. Support for other
  build systems is a separate change with its own gates.
- A consumer enables the binhost (`binrepos.conf`, `verify-signature = true`,
  the factory key lsigned in `/etc/portage/gnupg`) and, to rebuild from
  source, the overlay (`repos.conf`, `sync-type = zipfile`).
- `published_index` stays inert for gpkg: builds and verifies never read the
  factory's own binhost.
