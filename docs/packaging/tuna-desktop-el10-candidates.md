# Tuna Desktop candidates for EL10

`scripts/stage-tuna-desktop-el10.py` prepares recipes for six cells.
These cover skipjack/el10, albacore/alma10 and yellowfin/alma10-kitten on x86_64 and aarch64.
Each cell needs its own build in CI. AlmaLinux needs its own results.
The tool does not build or publish packages, change production pins, or qualify a release.

## Prerequisites

- Supply the recipe with the portal, cursor and file ownership declarations from PR818.
- Supply the factory contract with targets, adapters, capability maps and pinned images from PR819.
- Supply hashes for the source archive, vendor archive and Cargo.lock.
- Supply the receipt from the CI job that checked the vendor archive.

Missing prerequisites prevent output. The caller chooses the source explicitly; the tool does not select a final candidate.

## Command

Use paths to six local files. The tool does not download files.

```sh
python3 scripts/stage-tuna-desktop-el10.py \
  --recipe /owned/pr818/packages/tuna-desktop/package.yaml \
  --factory /owned/pr819/manifests/package-factory.yaml \
  --inputs /owned/candidate-inputs.json \
  --source-archive /owned/source.tar.gz \
  --vendor-archive /owned/tuna-desktop-vendor.tar.gz \
  --output /owned/new-el10-candidate
```

## Input identities

The JSON includes `source_sha`, `source_archive_sha256`, `vendor_archive_sha256`, `lock_sha256` and `vendor_url`.
Use 40 hexadecimal characters for `source_sha`.
The URL must identify an asset in the package repository on GitHub.
Release tags and assets can change; the archive hash binds the expected bytes.

The `vendor_receipt` includes `source_sha`, `lock_sha256`, `archive_sha256`, `run_id` and `offline_metadata_success: true`.
The positive run ID identifies the CI job that checked metadata offline with the lock.
The tool compares this receipt; it does not authenticate GitHub or rerun Cargo.
The caller must authenticate the original run and artifact before admission.

The `cells` array contains exactly six objects.
Each object includes `consumer`, `target`, `arch`, `platform`, `base_reference` and `base_image`.
Use the exact factory entry from `probe_images` for each architecture.
For a tagged probe, retain the tag in `base_reference` and its resolved digest in `base_image`.
CI must verify this resolution and the guest OS before the build.
Input bindings do not prove the identity of a live guest.

AlmaLinux v2 platforms and child digests must match their architecture.
Do not substitute Kitten or Stream for AlmaLinux.

## Output identities

The output contains six recipes and `candidate-inputs.json`.
The receipt binds hashes of the factory, recipe, input JSON, source, vendor, lock and each output recipe.
The tool hashes and parses the same bytes from each input document.
It rejects changed documents or archives before it writes output.
It also rejects an existing destination.

The recipes preserve declarations for six executables, schemas, portals, cursors and licenses.
Rust and compiler inputs use the factory maps for each target.
Do not substitute a CentOS toolchain map for AlmaLinux.

## Dependencies and installation

These lists name dependencies for native builds. They do not prove repository availability.
Each cell needs `pkgconfig(gtk4) >= 4.12` and `pkgconfig(gtk4-layer-shell-0) >= 1.1`.
A recipe for GTK3 does not supply GTK4.
No native provider for this GTK4 library has passed the cell checks yet.

The locked `gtk4-session-lock-sys` version 0.4.1 uses the same library.
Its `v1_1` feature needs version 1.1 or later; it does not use a separate library.
Cargo must accept the system APIs and ABI.
Missing providers or wrong CPU baselines prevent a build.

RPM derives dependencies from ELF.
The explicit dependencies also cover dconf, schemas, canberra, the sound theme and portals.
A native installation must prove their package owners and ABI.

## CI evidence and promotion

CI can build candidates after input admission.
It must retain the base digest, repository inputs, resolved NEVRAs, compiler and CPU evidence.
It must also retain the lock, offline build result, RPM digest and signature.
A pristine installation needs receipts for package ownership, modes, symlinks and smoke checks.

This receipt keeps `dependency_status` and `native_install_status` at `unmeasured`.
Later receipts must bind these input hashes.
The tool does not emit an ActionResult, publish a signed RPM or prove an installed session.

Promotion needs all 72 capabilities, visual checks, a GNOME comparison and hardware evidence.
Publication also needs a functional release on every variant.
These gates do not prevent a candidate build that supplies the evidence.
Issue828 and variant gates3195–3197 stay open until CI proves their requirements.
