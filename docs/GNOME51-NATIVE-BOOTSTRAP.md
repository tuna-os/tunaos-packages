# Native GNOME 51: closure and first publication

The native family must include every source under `src/gnome-51/`. Presence in
another family's build order does not meet this requirement. The regression in
`tests/test_gnome51_native_family_coverage.py` removes GOA or Orca from the
native order and proves that each omission fails. GOA must follow libadwaita
and precede the control center. The runtime image needs Orca even when the
base has an older version of that package.

Keep `gnome50-el10-compat`: GDM 51 needs that package by name. Its name does
not select Shell 50. Preserve its PAM, user-database, and SELinux support
until tests prove a replacement works.

## Local native bootstrap prerequisite

The first Orca probe (`/tmp/gnome51-orca-native.dMIgb1/build.log`) created its
SRPM in the Fedora runner, then failed in EL10's initial Mock root at
`%gnome_check_version`, before BuildRequires installation. A BuildRequires on a
new macro package cannot fix a spec that must parse before those dependencies
are installed. The native Orca, session, settings-daemon, and libadwaita specs
therefore use the existing Ptyxis convention: local source-version derivation
with an explicit prerelease-format check in `%prep`. The regression in
`tests/test_gnome51_release_versions.py` exercises that check with valid and
invalid version spellings. The changed-input build
`/tmp/gnome51-orca-native.5F9uN9/build.log` passed that stage and dependency
installation, then found base AT-SPI 2.56.1 below Orca's required 2.58.6.

`src/gnome-51/at-spi2-core` supplies the missing native predecessor. Build it
first, then Orca into the same isolated local repository. Its 2.58.6 source
requires GLib >= 2.67.4 and Meson >= 0.64.0, so this bounded probe does not need
the entire GLib/GTK bootstrap. The native full-stack order nevertheless places
it after GI and before its GTK/Orca consumers. Utah's recipe is the packaging
reference, not a binary source. Neither a single-package build request nor
source-graph coverage establishes complete desktop closure.

### Local result — 2026-10-30

AT-SPI 2.58.6 and Orca 51.0 both built in the native CentOS Stream 10 Mock root.
The runner digest was `ed61842fd5cf306c0907c6c31cc3f48cae415fef58e0b2c495c490b53521a5cb`.
The successful Orca build used the AT-SPI RPMs from the same isolated repository.
Its `%check` validated the desktop file. The AT-SPI spec has no `%check`;
this result does not claim an upstream AT-SPI test pass.

Receipts remain under `/tmp/gnome51-orca-native.5F9uN9`:
`at-spi2-core-build.log`, `orca-build.log`, result JSON and RPMs.
The earlier disk-full result is in `orca-no-space-receipt.tar.gz`.
Only the completed Flounder smoke preloads were removed to recover space.
No shared cache prune or pre-existing image removal occurred.

A real AlmaLinux 10 transaction then exposed a runtime dependency defect:
`orca --version` failed because the Gdk 3.0 typelib was absent. Orca now
requires `gtk3 >= 3.24` and `python3-gobject >= 3.18`, without architecture
qualifiers on the noarch package. The rebuilt RPM installed with its native
dependencies and printed `Orca version 51.0, AT-SPI2 version: 2.58.6`.
The native Atspi typelib and Python override loaded from the installed RPM.
GLib and SELinux policy package versions stayed unchanged.

The final Orca spec SHA-256 is
`ef0a897dd9945f7b5fa196602afabfe093daf840730d95cc3a2eb1160d36b822`.
The Orca RPM SHA-256 is
`68bbc5692844c4c8a2d2daca18d2f9797689967f3569f8dc9481953ecfc1eda1`.
The AT-SPI main RPM SHA-256 is
`e98256f5f4f06d9d859a08d8bd01830aed965f9270e6b1f8441b7b6f5b279eee`.

These unsigned local outputs prove two native package builds, their bounded
dependency installation and Orca CLI startup. They do not prove a frozen
full-stack build, signed publication, a live accessibility session or desktop
login. The GNOME 51 provider remains unqualified; no consumer pin changed.


## First-publication proposal (not implemented)

The [OCI rehearsal](R2-GHCR-MIGRATION.md) needs a signed seed that already
exists. A new GNOME 51 family cannot use that lane until it has such a seed.
Do not remove the seed check or treat a registry error as an empty repository.

A separate, reviewed bootstrap lane should:

1. Select one native family and architecture. Pin the source commit and a
   completed build run from a trusted source. Build the whole dependency order in the
   target's own root. Record base indexes and package versions. Do not import
   Hummingbird or GNOME 50 binaries to fill gaps.
2. Verify all input artifacts and reject partial chains. Assemble a new isolated
   candidate from those artifacts. Check architecture, source provenance, RPM
   checksums, and the full inventory of expected packages.
3. Apply the existing RPM and repodata signatures. Push only a unique candidate
   tag, then sign its immutable digest. Prefer the approved publisher identity
   if its scope covers this operation. Otherwise review the new signer and
   consumer trust explicitly. Repository membership alone is not trust.
4. Pull that exact digest on a fresh native runner. Verify both signature layers.
   Resolve and install the desktop packages for TunaOS with signature checks
   enabled. Test GDM login, session, Shell/Mutter, portals, audio, and
   accessibility. Test with SELinux set to `enforcing`. Test each enabled architecture.
5. Compare the full stack with the source and package inventory in
   [Utah](https://github.com/projectbluefin/utah-packages). Include GJS/mozjs,
   GLib/GI, GTK/libadwaita, schemas, session/GDM, accounts, portals, and library
   ABIs. Versions need not all be 51. Utah's Hummingbird payload is a reference,
   not an EL10 binary provider. Test the metadata and behavior of installed extensions;
   a Shell version alone is insufficient.
6. Review the evidence for promotion. Under the same publication lock, verify
   the live family tag is still absent before a first publication. Fail on an
   existing tag, authentication error, or ambiguous registry response. Never
   overwrite a publication that another job added. Preserve the candidate digest and
   evidence even if promotion fails.
7. Move TunaOS consumer pins only to verified immutable outputs, with explicit
   architecture mapping. Rebuild and boot the images before promotion. Keep a
   known-good rollback digest and the old HTTP routes for installed systems.

This design note grants no permission to change workflows, credentials, trust
policies, or live tags, or to dispatch jobs or publish packages.

## Remaining migration work

The native queue defaults select the GNOME 51 order. Retire explicit GNOME 50
cells, legacy manifests, and release-track labels in a separate change. Test
the planner and downstream consumers with that change. Do not equate source
coverage with built packages, published dependency closure, or an operational
all-GNOME-51 image. Keep the TunaOS floor change blocked until the providers
and image qualification above exist.
