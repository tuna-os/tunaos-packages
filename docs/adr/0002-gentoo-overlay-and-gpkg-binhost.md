# ADR 0002: Gentoo as a factory target — rendered overlay plus signed gpkg binhost

- Status: accepted
- Date: 2026-10-16
- Tracking issue: [#838](https://github.com/tuna-os/tunaos-packages/issues/838)
- Sign-off: hanthor (maintainer) delegated approval for the Tuna Desktop
  v1.0.0-beta.1 rollout (tuna-desktop ADR 0008)
- Policy: tunaOS RFC lifecycle (ADR 0004): a new package format and publish
  route is a package-sourcing change, so it is recorded here.

## Context

guppy (tunaOS Containerfile.gentoo) is a Gentoo image that consumes only
`::gentoo` and Gentoo's official binhost. To ship tuna-desktop to it, the
factory needs a Gentoo package path. Before this, the factory had none: no
gentoo target in `manifests/package-factory.yaml`, no renderer in
`scripts/tideforge.py`, no gpkg branch in the cell runner, and no publisher.
Every tuna-desktop dependency is already stable on amd64 in `::gentoo`
(gtk4-layer-shell 1.3.0, gtk 4.20, libadwaita 1.8.7, Rust 1.98.1).

## Decision

1. **The recipe stays the source of truth.** `tideforge.py render --target
   gentoo` writes a one-package overlay (`profiles/repo_name` = `tunaos`,
   `masters = gentoo`, thin Manifests) with an EAPI 8 ebuild at
   `<outputs.gpkg.category>/<name>/<name>-<PV>.ebuild`. The recipe version
   maps to PMS (`1.0.0~beta.1` → `1.0.0_beta1`), SPDX licenses map to Gentoo
   names, and the recipe's own `custom` build and install commands run
   unchanged. A recipe that needs the `rust` capability inherits
   `cargo.eclass`: its `RUST_DEPEND` (at `build.rust_version`, as
   `RUST_MIN_VER`) is that capability's Gentoo spelling, and the build runs
   under `cargo_env`. Sources are the recipe's pinned archives, renamed
   `${P}-<filename>` so an unversioned asset cannot collide in a shared
   DISTDIR; the Manifest is generated from the bytes the recipe's sha256
   already verified.
2. **One `gentoo` target, format `gpkg`.** The cell emerges the overlay's
   ebuild inside the desktop/systemd stage3 (the profile guppy converts to),
   taking dependencies from `::gentoo` and Gentoo's signed binhost, and
   keeps the gpkg. Its verify installs that gpkg into a fresh stage3 with no
   overlay, so the package can only arrive as the binary under test.
3. **Publish a signed binhost and the overlay.** `publish-tideforge-gentoo.yml`
   signs each gpkg with the factory key (`gpkg-sign`), regenerates `Packages`
   from the whole tree (`emaint binhost --fix`), and serves it at
   `https://repo.tunaos.org/gentoo/binpkgs/<arch>/`. The overlay of every
   recipe targeting gentoo is served as one zip at
   `https://repo.tunaos.org/gentoo/overlay/tunaos-overlay.zip` for
   `sync-type = zipfile`. Its verify trusts the factory key the way getuto
   trusts Gentoo's, requires signatures on every binary package, and
   installs from the served URL.

**Considered options:**

- **A hand-written ebuild in a separate overlay repository**: rejected. It
  duplicates the recipe's sources, commands and install layout, so the
  Gentoo build would drift from the eight targets that render from it.
- **Ship only an overlay and let guppy compile**: rejected. guppy builds
  from binaries because @world from source does not fit its CI time; a
  Rust build of tuna-desktop in every image build is the same cost again.
- **Rendered overlay plus signed binhost**: chosen.

## Consequences

- The ebuild renderer supports `build_system: custom` only, the one
  tuna-desktop uses; other build systems fail rather than render untested
  ebuilds. Widening it is a separate change with its own gate cells.
- A consumer enables the binhost (`binrepos.conf`, `verify-signature = true`,
  the factory key lsigned in `/etc/portage/gnupg`) and, to rebuild from
  source, the overlay (`repos.conf`, `sync-type = zipfile`).
- `published_index` stays inert for gpkg: builds and verifies never read the
  factory's own binhost.
