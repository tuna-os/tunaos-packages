# Tideforge build-once intermediate experiment

**Status:** theory branch only; not proposed for production or merge  
**Experimented:** 2026-08-26  
**Prototype:** `scripts/tideforge-intermediate.py`

## Short answer

Tideforge can compile a useful subset **once per CPU architecture**.
It wraps the staged filesystem into RPM, DEB, and Pacman packages without a second compilation step.
Pure data packages can build once for all architectures.

Tideforge cannot safely use one distribution-agnostic buildroot for the whole catalog.
The desktop libraries and compositors bind to target ABIs, headers, filesystem layouts, and compiler policy.
The reuse of those binaries across targets leads to failures of the ABI.

The useful design uses **one portable action for payloads and fast actions for packages and gates**.
It does not eliminate target roots.

```text
checksum-pinned source + portable SDK + architecture
                         |
                         v
              staged filesystem (TFI)
                         |
           +-------------+-------------+
           |             |             |
           v             v             v
       RPM metadata   DEB metadata   PKGBUILD metadata
       + native gate  + native gate  + native gate
```

## What was measured

The catalog contains 45 Tideforge recipes, 37 of them multi-target:

| Build system | Recipes | Multi-target | Initial reuse assessment |
| --- | ---: | ---: | --- |
| custom | 17 | 14 | Mostly target-linked COSMIC/Rust; unsafe by default |
| meson | 7 | 4 | Target-linked C/C++ libraries; unsafe by default |
| cmake | 6 | 5 | Mixed; header/data-only installs may qualify |
| cargo | 5 | 5 | Must prove native dependency and symbol contract |
| go | 5 | 4 | Strong candidate when `CGO_ENABLED=0` and static ELF is enforced |
| data | 4 | 4 | Safe first cohort; architecture-independent |
| autotools | 1 | 1 | `libunwind`; explicitly target ABI-sensitive |

The immediate candidate cohort includes:

- all four `data` recipes: `dms`, `dms-greeter`, `oversteer-udev`, and `wayland-protocols`;
- the four multi-target Go recipes (`danksearch`, `dgop`, `dms-cli`, `uupd`) with static binaries and no CGO;
- selected header or icon recipes after manual review (for example `cli11-devel` and `pop-icon-theme`).

That gives eight recipes with high confidence today.
This path is a useful optimization, but not a replacement for native build chains.

## Prototype result

The prototype seals a staged root into a deterministic archive file with:

- normalized file ownership and timestamps;
- package, source, and build-contract identity;
- an inventory with path, mode, size, and SHA-256;
- the digest of the complete payload tree;
- `DT_NEEDED` libraries and GLIBC symbol versions for each ELF binary.

The test staged `dms-greeter` once from its verified upstream source on x86_64.
The payload contained 868 regular files across 957 inventory entries.
Both runs produced the exact same output:

```text
TFI archive SHA-256: 38b24235600b2560c70ac536d32d53ec372fcd4ca4b823e2b1774ce1388c5d6d
Payload tree SHA-256: db46f0b9b6fbb864e58629b8535924831cdc89f4fa0c519a4bae73bd43b95ad7
Targets planned:      el10, ubuntu, debian, opensuse-tumbleweed, arch
Compile per target:   false for all five
```

This proves the byte-reuse boundary for data packages.
It does not prove package creation, signature checks, or installation on production targets.

## Why the full “one root per CPU” theory breaks

1. **The buildroot is an ABI input.**
   A binary records the shared libraries and versioned symbols it needs.
   A build against a newer glibc or library produces binaries that fail on an older target.
   The TFI prototype records these dependencies so that the tool rejects targets that fail.

2. **Library destinations differ.**
   Existing recipes use `/usr/lib64` for RPM and multiarch paths under `/usr/lib/<tuple>` for Debian.
   A single immutable payload of libraries cannot fit both layouts without modification.

3. **Package metadata is native.**
   RPM dependency generators, `dpkg-shlibdeps`, and Pacman lists of dependencies differ.
   Subpackage splits, ldconfig rules, tmpfiles setup, and maintainer scripts also differ.
   Tideforge history shows that these differences matter for package health.

4. **Build policies differ across distributions.**
   Security flags, debug packages, LTO, Python paths, and FFI rules differ per distribution.
   A portable SDK replaces those policies with a new ABI policy.
   It does not eliminate distribution differences.

5. **Validation must run per target.**
   Even a portable executable can fail a target dependency or integration check.
   To remove target roots from validation violates the promotion contract.

## Proposed eligibility contract

Tideforge selects its portable-payload handler only when all conditions are true:

1. a workflow declares an immutable SDK root with a digest pin;
2. the build captures installation under a normalized `DESTDIR`;
3. the output contains no undeclared absolute RPATH/RUNPATH or host paths;
4. every ELF interpreter and symbol ceiling matches the ABI of each target;
5. target tools change metadata and path mappings without changes to ELF bytes;
6. CI lints, installs, and tests the native package on every target;
7. any target failure demotes the recipe to normal target builds.

The package handler retains this logic.
The `data` type is a noarch candidate.
The Go handler qualifies per CPU architecture when the environment disables CGO.
Tideforge reuses Go output only when the ELF binary has no interpreter or `DT_NEEDED` entries.
Other build systems remain target-native.
A candidate build that fails proof falls back to the standard target path.

The action key for this build omits the target ID and native format.
It includes source digests, architecture, SDK digest, compiler flags, and dependency keys.
Separate target keys include the TFI digest, target contract, renderer, and native dependencies.

## Sensible next experiment

Do not start with GNOME, COSMIC, `libseat`, `xfconf`, or `libunwind`.
Start with two cohorts:

1. produce the four data recipes from one `noarch` TFI across native adapters, then run clean-install checks;
2. set `CGO_ENABLED=0` and empty `DT_NEEDED` dependencies on one Go recipe, then package the bytes for all targets.

Measure execution time and cache storage against standard multi-target cells.
Only after those pass should teams test libraries that link dynamically.

## Related prior art

- [nFPM](https://nfpm.goreleaser.com/docs/) bundles files into several formats of native packages.
  Its documentation notes that it covers a simplified feature set.
- [Flatpak](https://docs.flatpak.org/en/latest/introduction.html) gives distribution independence through a shared runtime.
  This validates portable runtimes, but differs from native packages for a desktop OS.
- The [documentation for the dynamic linker](https://www.sourceware.org/glibc/manual/2.44/html_node/Dynamic-Linker-Hardening.html)
  explains how to inspect symbols with `readelf`.
  The prototype records these symbol dependencies in each TFI manifest.

## Non-goals of this branch

- no workflow integration;
- no production publisher or R2 changes;
- no change to `main` or RFC 011;
- no relaxation of target build or runtime gates;
- no claim that generated RPM, DEB, or Arch packages are interchangeable.
