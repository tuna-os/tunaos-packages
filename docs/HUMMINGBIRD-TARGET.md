# Targeting Hummingbird

This document records what "build for Hummingbird" means in this repository.
It provides measurements behind the architecture decisions.
Written 2026-09-02 after comparison with [projectbluefin/utah-packages](https://github.com/projectbluefin/utah-packages).
That repository built GNOME 51 for Hummingbird rapidly.
Every number comes from primary RPM metadata.
Reproduce with `scripts/check-hummingbird-installability.py` and `scripts/gap_engine.py --catalog manifests/hummingbird-desktops.yaml`.

Hummingbird upstream documentation remains authoritative over this document.
Refer to [hummingbird-project.io/docs](https://hummingbird-project.io/docs/) and [gitlab.com/redhat/hummingbird](https://gitlab.com/redhat/hummingbird).

## 1. Hummingbird ABI definition

Index measurements on 2026-09-02 for x86_64:

| package | hummingbird | fedora 44 | rawhide |
|---|---|---|---|
| glibc | 2.43-8.4.hum1 | 2.43-2.fc44 | 2.44.9000-1.fc46 |
| openssl-libs | 3.5.8-0.1.hum1 (`libcrypto.so.3`) | 3.5.5-1.fc44 (`.so.3`) | 4.0.2-1.fc46 (**`.so.4`**) |
| python3 | 3.14.7-1.hum1 | 3.14.3-2.fc44 | 3.15.0~rc1-1.fc45 |
| perl-libs | 5.42.2-525.hum1 | 5.42.1-523.fc44 | 5.44.0-527.fc45 |
| libxml2 | 2.15.3-0.1.4.hum1 | 2.12.10-6.fc44 | 2.13.9-4.fc45 |
| glib2 | 2.89.3-1.hum1 | 2.88.0-1.fc44 | 2.89.4-1.fc46 |
| harfbuzz | 14.3.1-1.hum1 | 12.3.2-1.fc44 | 14.4.0-1.fc46 |

Two facts hold simultaneously:

- Packages that Hummingbird rebuilds (glib2, harfbuzz, gcc, rust, meson) match Rawhide versions.
- Packages that define the ABI (glibc, openssl, python, perl) match Fedora 44.

Hummingbird provides an overlay of rebuilt packages on a pinned Fedora release.
Its build root composes Fedora 44 repositories with Hummingbird Pulp repositories at higher priority.
Therefore, the factory uses **Fedora 44 plus public-hummingbird at high priority** for `mock/hummingbird-ci*.cfg`.

## 2. Cost of the Rawhide root

The build root for Rawhide caused errors in binary packages.
Inspection of `repo.tunaos.org/hummingbird/20251124-x86_64` (8738 binaries) shows:

```text
libm.so.6(GLIBC_2.44)(64bit)   needed by gstreamer1-plugins-good, libavutil-free, zvbi
```

These three packages cannot install on Hummingbird because Hummingbird provides glibc 2.43.
Utah-packages encountered the same issue with `libcrypto.so.4` and moved to Fedora 44 roots.
Earlier configurations tried to patch Fedora 44 per namespace.
They assumed that Hummingbird would catch up to Rawhide.
Hummingbird does not follow Rawhide for ABI versions.

## 3. Resolving published packages on Hummingbird

Consumers test for installability before acceptance.
Static evaluation on 2026-09-02 for declared roots without pre-installed packages:

| repositories enabled | gnome roots absent | closure | unresolved capabilities |
|---|---|---|---|
| hummingbird alone | 53 of 58 | 94 | 5 |
| hummingbird + **utah-packages** (Pages mirror, 421 binaries) | 33 | 208 | 185 |
| hummingbird + **tunaos-hummingbird** (8738 binaries) | 3 | 602 | **30** |
| hummingbird + Fedora 44 (if a consumer enabled it) | 0 | 695 | 0 |

No desktop resolves across our prefix today: gnome 30 unresolved, kde 25, cosmic 9, niri 28, xfce 18.
Primary blockers:

- The `GLIBC_2.44` dependency in multimedia packages;
- `gdm` needs `gnome50-el10-compat`, which is specific to EL10;
- `gtk4` needs `libgstplay-1.0.so.0` from `gstreamer1-plugins-bad-free`;
- Missing desktop libraries (libcanberra, evolution-data-server, webkitgtk6.0, samba);
- Roots absent from both repositories (`fprintd-pam`, `gnome-disk-utility`, `gnome-user-share`).

`scripts/check-hummingbird-installability.py` runs daily as an advisory check.
It becomes a required gate when desktops resolve.

## 4. Comparison with utah-packages

| Dimension | utah-packages | tunaos-packages before | after 2026-09-02 |
|---|---|---|---|
| build root | Fedora 44 + public-hummingbird (priority) | Rawhide + hummingbird + three F44 pins | Fedora 44 + public-hummingbird + our prefix |
| recipes | Rawhide dist-git, pinned by commit | Rawhide dist-git, imported at build time | unchanged |
| sources | upstream release tarballs, SHA-512 locked | dist-git lookaside | unchanged |
| what gets built | curated list (193 sources) | measured runtime closure (670 sources, 18 tiers) | unchanged |
| staging | 5 stages, each a local repo for the next | tiers over one topological order, `[local-build]` | unchanged |
| disttag | `.hum1.bfin` (sorts above `.hum1`) | `.bfin1` (sorts below `.hum1`) | unchanged |
| publish | OCI image with digest | R2 prefix behind a Worker | unchanged |
| **installability gate** | dnf transaction inside bootc-os before publish | none | static checker; container gate is next step |
| preflight | resolves BuildRequires in real root | `scripts/preflight-buildrequires.py` | unchanged |

The build root and the gate verify reliability.

## 5. Closure size across references

Comparison of the closure against Rawhide and Fedora 44:

| reference | gnome sources to build | kde | cosmic | niri | xfce |
|---|---|---|---|---|---|
| Rawhide (committed) | 301 | 375 | 175 | 297 | 268 |
| Fedora 44 | 311 | 378 | 152 | 306 | 256 |

Hummingbird ships no desktop stack, so core libraries remain missing in either comparison.
The factory retains Rawhide as reference because package recipes derive from Rawhide specs.

Utah-packages built a narrower scope: a single desktop based on Bluefin requirements.
This repository provides five desktop choices.

## 6. Known open items

- **Rawhide recipes on Fedora 44 hit version floors:**
  gcc 16 and rust 1.97 in Hummingbird satisfy the toolchain.
  Version requirements for libraries appear in `preflight-buildrequires.py` and enter the build order.

- **Packages with GLIBC_2.44 need rebuilds:**
  The factory must rebuild these packages and re-verify symbols.

- **Name collisions occur in repository overlays:**
  Base repositories can provide a package with a newer soname.
  Older packages from Fedora 44 then fail to resolve.
  The factory excludes the Fedora name or builds against the base.

- **Container validation gate:**
  The next step runs `dnf --assumeno install` inside `quay.io/hummingbird-community/bootc-os`.
  Workflows for publication block waves that fail this check.

## 7. Strategic integration decision

TunaOS consumes `ghcr.io/projectbluefin/common` and `ghcr.io/ublue-os/brew` by digest.
`ghcr.io/projectbluefin/utah-packages` provides Bluefin's GNOME 51 for Hummingbird.
Duplicate GNOME 51 packages in this repository waste resources.

The architecture split:

- **Consume utah-packages for GNOME on Hummingbird:**
  TunaOS points at the OCI image by digest.
- **Build non-GNOME desktops here:**
  This factory builds KDE, COSMIC, Niri, and XFCE in the Fedora 44 root with install gates.
- **Share component audits:**
  Both factories reference `components:` in `manifests/hummingbird-desktops.yaml`.

## 8. Implementation results (2026-09-02)

The architecture split is active on both sides:

- **tunaOS (`hummingbird:gnome`):**
  `image-versions.yaml` pins `ghcr.io/projectbluefin/utah-packages` by digest.
  Container builds mount `/repository` at `/run/utah-packages` as a `file://` repository.

- **This factory:**
  `manifests/package-factory.yaml` lists the digest in `consumed_indexes` on the hummingbird target.
  `scripts/oci_repository.py` reads repodata from the registry.
  The gap engine treats the packages from utah as present.
  The container gate runs tests with `dnf --assumeno`.

Package count impact across desktop targets:

| desktop | sources to build, before | after | Δ |
|---|---|---|---|
| gnome | 301 | 248 | −53 |
| kde | 375 | 353 | −22 |
| cosmic | 175 | 115 | −60 |
| niri | 297 | 252 | −45 |
| xfce | 268 | 115 | −153 |
| bluefin (parity set) | 392 | 186 | −206 |
| **build order, packages** | **778** | **664** | **−114** |

Utah provides base platform libraries (gtk4, glib2, pipewire, mesa).
This reduces the closure size across desktops.

The index for openSUSE Tumbleweed is valid on both architectures.
Targets for APT serve amd64 and arm64 packages.
EL10, Fedora, and Hummingbird maintain aarch64 build legs.
Arch aarch64 lacks a build leg and consumer.

## 9. First build results on the Fedora 44 root

Run 33597624514 was the initial build on the Fedora 44 root.
The build reached the four-hour deadline with healthy workers:
x86_64 built 132 packages, aarch64 built 189 packages, and three packages failed:

| package | leg | what dnf or rpmbuild said | on main's Rawhide root |
|---|---|---|---|
| `langtable` | both | `%check`: `xmllint: command not found` | identical |
| `python-dbus-next` | aarch64 | dependency conflicts with pytest versions | deferred |
| `python-aiohappyeyeballs` | aarch64 | dependency conflicts with pytest versions | deferred |

**langtable** failed because the buildroot lacked libxml2.
`src/hummingbird/langtable/` now vendors the spec with `BuildRequires: libxml2`.

**pytest-asyncio** failed because Fedora 44 provides pytest-asyncio with a dependency on `pytest < 9`, while Hummingbird provides pytest 9.1.1.
To avoid dependency downgrades, the factory builds Rawhide pytest-asyncio 1.4.0 in `bootstrap-04`.

Rule: **When a Fedora 44 BuildRequires tool conflicts with target packages, the factory builds the tool from the Rawhide reference.**
`tests/test_the_first_fedora44_legs_taught_two_buildroot_gaps.py` tests both fixes.
