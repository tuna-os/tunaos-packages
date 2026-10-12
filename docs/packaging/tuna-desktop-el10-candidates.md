# Tuna Desktop EL10 candidate inputs

`scripts/stage-tuna-desktop-el10.py` stages independent native RPM recipe
inputs for skipjack/el10, albacore/alma10, and yellowfin/alma10-kitten, each
on x86_64 and aarch64. It does not build, publish, update the production
source pin, or qualify a release. Builds run in CI against each admitted
base; a single CentOS RPM result cannot qualify the AlmaLinux cells.

Prerequisites:

- The common portal and licensed cursor installation/file ownership additions
  from PR818 must be present in the input recipe.
- The native target, consumer adapter, dependency capability and immutable
  AlmaLinux child image definitions from PR819 must be present in the input
  factory contract. Missing definitions fail before staging any recipes.
- Explicit candidate source archive, vendor archive and Cargo.lock hashes,
  plus the retained CI vendor receipt, must agree. No source is assumed to
  be the final release candidate.

Invoke with six explicit local input paths (nothing is downloaded):

```sh
python3 scripts/stage-tuna-desktop-el10.py \
  --recipe /owned/pr818/packages/tuna-desktop/package.yaml \
  --factory /owned/pr819/manifests/package-factory.yaml \
  --inputs /owned/candidate-inputs.json \
  --source-archive /owned/source.tar.gz \
  --vendor-archive /owned/tuna-desktop-vendor.tar.gz \
  --output /owned/new-el10-candidate
```

The JSON input contains `source_sha` (40 hexadecimal characters),
`source_archive_sha256`, `vendor_archive_sha256`, `lock_sha256`, and an
hash-bound `vendor_url` under the package repository's GitHub release assets.
`vendor_receipt` retains `source_sha`, `lock_sha256`, `archive_sha256`, a
positive integer `run_id`, and `offline_metadata_success: true` from the
actual locked offline vendor metadata job. This tool compares the supplied
receipt; it does not authenticate GitHub or independently rerun Cargo.
The CI caller must authenticate the producing run/artifact before admission.
GitHub release tags and assets can change; the URL is not inherently
immutable. The verified archive SHA256 binds the admitted bytes.

`cells` is exactly six objects, each containing `consumer`, `target`, `arch`,
`platform`, `base_reference` and `base_image`. Use the factory target's exact
per-architecture `probe_images` entry as both references where provided.
For a tagged probe such as CentOS Stream, retain that tag as `base_reference`
and its CI-resolved immutable digest as `base_image`; CI must verify the
resolution and guest OS identity before building. A tag/digest assertion
supplied here is an input binding, not evidence of the running base.
AlmaLinux v2 platforms and child digests cannot be exchanged between
architectures or substituted with Kitten or Stream.

The output contains six recipes and `candidate-inputs.json`, binding hashes
of the factory, original recipe, input JSON, source, vendor, lock and every
staged recipe. Input documents are read once and their parsed bytes hashed;
changed documents or archives are rejected before any output is written.
An existing output directory is rejected. Staging preserves
the source recipe's six executable, schema smoke, portal, cursor and license
payload rules. Rust and compiler inputs use the native factory capability
mappings; do not substitute the CentOS mapping for an AlmaLinux toolchain.

## Unmeasured dependency and installation gates

Dependency lists are proposed native inputs, **not a repository availability
claim**. In particular `pkgconfig(gtk4-layer-shell-0) >= 1.1` is required; the existing
GTK3 `gtk-layer-shell` recipe cannot satisfy it. No native GTK4 layer shell
provider has yet been demonstrated for these cells. The upstream locked
`gtk4-session-lock-sys`0.4.1 crate (archive checksum from Cargo.lock verified)
uses the same pkg-config library and requires >= 1.1 for the enabled
`v1_1` feature; it is not a separate GTK3 or session lock library. GTK4 >= 4.12 is explicit;
Cargo's actual system-library checks must also succeed. A missing provider,
wrong API/ABI or CPU baseline is a blocking build input, never a fallback.
RPM-generated ELF Requires complement explicit dconf, GSettings schemas,
canberra player/Pulse backend, freedesktop sound theme and portal routing.
Native installation must verify their resolved package owners and ABI too.

The factory CI can build candidates after these input admissions. It must
retain the immutable base digest, exact repository inputs/resolved NEVRAs,
compiler and CPU admission, Cargo.lock/offline build outcome, RPM digest and
signature, and pristine install package database/ownership/mode/symlink and
smoke receipts. Each cell remains `dependency_status: unmeasured` and
`native_install_status: unmeasured` in this staging receipt; later evidence
must be a separate receipt bound to these hashes. Staging is not an
ActionResult, signed RPM publisher, or installed-session proof.

The final 72-capability, visual, GNOME comparison, hardware and all-variant
working-release gates govern promotion and publication. They must not block
building the candidate needed to obtain those proofs. Issue828 and the
variant installed-state gates3195–3197 remain open until actual CI evidence
satisfies them; this scaffold does not close those requirements.
