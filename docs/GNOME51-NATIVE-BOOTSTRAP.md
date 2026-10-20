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
