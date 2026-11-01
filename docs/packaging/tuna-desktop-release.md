# Pointing tuna-desktop at a release tag

`packages/tuna-desktop/package.yaml` builds one tag of
[tuna-os/tuna-desktop](https://github.com/tuna-os/tuna-desktop). Six fields
move together for each tag: `version`, `release`, `source`, the vendored crate
asset, `build.environment.TUNA_VERSION` and the verify smoke's `want=`.
`scripts/prepare-tuna-desktop-release.py` sets all six, and
`tests/test_prepare_tuna_desktop_release.py` fails when they name different
tags.

## Version forms

Each format shows tag `vX.Y.Z-pre.N` (for example `v1.0.0-beta.1`) as follows:

| Where | Value | Why |
| --- | --- | --- |
| `TUNA_VERSION` | `v1.0.0-beta.1` | The tag. Every binary's `--version` prints it without the `v`. |
| verify `want=` | `1.0.0-beta.1` | What the smoke expects from `--version`. |
| recipe `version`, RPM `Version:` | `1.0.0~beta.1` | RPM sorts `~` before the release (`1.0.0~beta.1 < 1.0.0`). RPM forbids `-` in `Version:`. |
| Debian upstream version | `1.0.0~beta.1` (package `1.0.0~beta.1-1`) | dpkg sorts `~` first in the same way. |
| pacman `pkgver` | `1.0.0beta.1` | pkgver forbids `~` and `-`. libalpm already sorts a trailing alphabetic segment older (`1.0.0beta.1 < 1.0.0`). Upstream's `packaging/arch/PKGBUILD` uses the same value. |

Tideforge renders the pacman value with `tideforge.pacman_version()`.
`validate` only accepts versions that are valid in all three formats. A `~`
must be followed by a letter, because `1.0~1` would become `1.01` for
pacman, which sorts after `1.0`.

## Filling a new tag

Run this after the tag is pushed upstream, from a clean checkout of this repo
with `cargo`, `gh` (authenticated, allowed to create releases) and network
access:

```sh
python3 scripts/prepare-tuna-desktop-release.py v1.0.0-beta.1 --publish
python3 -m pytest -q tests/test_prepare_tuna_desktop_release.py tests/test_tideforge.py
python3 scripts/tideforge.py validate packages/tuna-desktop/package.yaml
```

The script:

1. Downloads `archive/refs/tags/<tag>.tar.gz` and records its sha256. It checks that the top-level directory is `tuna-desktop-<tag without v>`.
2. Runs `cargo vendor --locked --versioned-dirs` in the extracted tag. It packs `vendor/` as a deterministic tar: `--sort=name --format=gnu --owner=0 --group=0 --numeric-owner`, normalized modes, the tag commit's time as `--mtime`, and `gzip -n -9`. It packs twice and stops if the two hashes differ.
3. Extracts the tag archive again into a fresh directory, unpacks the vendor tar there, and writes the recipe's own `cargo_config` to `.cargo/config.toml`. Then it runs `cargo metadata --locked --offline`.
4. Looks for release `tuna-desktop-vendor-<tag>` in this repo. With `--publish` it creates the release when it is missing. In either case it downloads the published asset and needs the same sha256 as the local build. Without `--publish`, a missing release stops the script before the recipe changes.
5. Rewrites only the six fields and deletes the `# PLACEHOLDER:` comments. Each edit must match exactly once. Then it validates the result with Tideforge and the tag-consistency check before it writes the file.

Use `--keep DIR` to keep the downloads and the vendor tar for inspection. Run
the script again with the same tag to check an existing release: it rebuilds
the asset and compares it with the published one.

Commit the recipe change. Then let the factory build the `tideforge-tuna-desktop-*`
cells: `arch-x86_64`, `fedora`, `rawhide`, `eln`, `el10` and `opensuse-tumbleweed`
on `x86_64` and `aarch64`, `ubuntu` (resolute), `debian` (sid) and
`trixie` on `amd64` and `arm64`, and `gentoo-amd64`.
Each smoke runs every binary and checks `--version` against `want=`.

## Colour runtime dependency contract

Every target explicitly requires the **colord daemon**, not just its client
library, and **GNOME Color Manager** for Settings' `gcm-viewer` profile view.
The ColourManager uses colord's system D-Bus service;
neither that service nor a session helper is discoverable through ELF
dependency scanning. These are hard runtime dependencies, including on
minimal images that omit recommended packages.

Native package identities are `colord` and `gnome-color-manager` on Arch,
Fedora/Rawhide/ELN/EL10, Debian sid/trixie, Ubuntu resolute and Tumbleweed.
Gentoo uses `x11-misc/colord` and `gnome-extra/gnome-color-manager`.
The source-backed package metadata is available from:

- [Arch Extra](https://archlinux.org/packages/extra/x86_64/gnome-color-manager/)
  (also declares the `colord` daemon dependency).
- [Fedora dist-git](https://src.fedoraproject.org/rpms/gnome-color-manager/raw/rawhide/f/gnome-color-manager.spec)
  and this factory's `manifests/catalog.yaml` runtime entries for both packages.
- [EL10 AppStream package metadata](https://rpmfind.net/linux/rpm2html/search.php?query=gnome-color-manager&system=almalinux&arch=)
  and [colord](https://www.rpmfind.net/linux/RPM/almalinux/10.2/appstream/s390x/colord-1.4.7-6.el10.s390x.html).
- [Debian trixie](https://packages.debian.org/trixie/gnome-color-manager)
  (also links sid and identifies `colord` as the system daemon).
- [Ubuntu resolute](https://packages.ubuntu.com/resolute/gnome-color-manager).
- [openSUSE package metadata](https://software.opensuse.org/package/gnome-color-manager).
- Gentoo's upstream metadata for
  [colord](https://raw.githubusercontent.com/gentoo/gentoo/master/x11-misc/colord/metadata.xml)
  and [GNOME Color Manager](https://raw.githubusercontent.com/gentoo/gentoo/master/gnome-extra/gnome-color-manager/metadata.xml).

`tests/test_tideforge.py::test_tuna_desktop_native_runtime_includes_colour_services`
checks actual rendered `Requires`, `Depends`, pacman `depends`, and Gentoo
`RDEPEND` for every declared target. Dependency-only corrections do not change
the six release fields above; qualification and publication remain separate.

GNOME 51 calibration does not launch `gcm-calibrate`. Its
[`panels/color/cc-color-calibrate.c`](https://github.com/GNOME/gnome-control-center/blob/51.0/panels/color/cc-color-calibrate.c)
calls `org.gnome.Mutter.ColorManager.CalibrateMonitor` and the returned
session's gamma-LUT methods, then starts colord's session-bus ColorHelper.
The candidate compositor implements the former in `color_manager.rs`;
`runtime/color.rs` applies and reports its calibration state.
The [native Debian colord payload](https://packages.debian.org/trixie/amd64/colord/filelist)
provides `/usr/libexec/colord-session`,
`/usr/share/dbus-1/services/org.freedesktop.ColorHelper.service` and
`/usr/lib/systemd/user/colord-session.service`, alongside the system daemon.
The [manager payload](https://packages.debian.org/trixie/amd64/gnome-color-manager/filelist)
contains `gcm-import`, `gcm-inspect`, `gcm-picker` and `gcm-viewer`, not
`gcm-calibrate`; that absent executable is not this GNOME 51 workflow's
dependency.
GNOME 51's
[`panels/color/cc-color-panel.c`](https://github.com/GNOME/gnome-control-center/blob/51.0/panels/color/cc-color-panel.c)
function `gcm_prefs_profile_view` spawns `gcm-viewer --profile <id>
--parent-window <handle>` through `G_SPAWN_SEARCH_PATH`; toolbar refresh gates
the View button on `g_find_program_in_path ("gcm-viewer")`. This is the
manager package's concrete consumer. Profile import instead calls
`cd_client_import_profile_sync` directly, not `gcm-import`.


## RPM targets

The `fedora`, `rawhide`, `eln`, `el10` and `opensuse-tumbleweed` build lists use
`pkgconfig()` names, which resolve the same way on all five (zypper installs
by capability as dnf does). Fedora 44, Rawhide, ELN and Tumbleweed supply all
dependencies. `rawhide` is its own target (buildroot `fedora:rawhide`, served
at `rpm/rawhide/{arch}`) because `fedora` builds in a Fedora 44 root, whose
packages cannot qualify bonito-rawhide.
On `el10`, the factory supplies `gtk4-layer-shell-devel` from
`packages/gtk4-layer-shell`. The build gets it from the published index of the
target, so `publish-tideforge-rpms.yml` must publish that package for `el10`
before the `tuna-desktop` `el10` cells can build. Tumbleweed's runtime
`Requires` use its shared-library package names (`libgtk-4-1`,
`libgtk4-layer-shell0`, `Mesa-libEGL1`, ...). Publish each target with
`publish-tideforge-rpms.yml`, `packages: tuna-desktop`, `target: <target>`.

## Deb targets

The `ubuntu` and `debian` lists use the same Debian package names: resolute
and sid ship every build dependency, including `libgtk4-layer-shell-dev` and a
`rustc` newer than 1.92. The runtime `Depends` repeat upstream's own `.deb`
(`scripts/tuna-release` in tuna-desktop) next to `${shlibs:Depends}`, plus
`dbus-bin` for `dbus-update-activation-environment`. Publish with
`publish-tideforge-debs.yml`, `packages: tuna-desktop`.

`trixie` (Debian 13) ships gtk4-layer-shell 1.0.4, below the 1.1 floor.
The factory does not replace that system library, because every consumer on
the image would then get the new one. Instead, the `trixie` build compiles a
private gtk4-layer-shell 1.3.0 (the `sources` entry with `targets: [trixie]`).
The library goes in `/usr/lib/<multiarch>/tuna-desktop/`, and `patchelf` sets
a RUNPATH on the binaries that link it. The toolchain is trixie's
`rustc-web` 1.96, not its `rustc` 1.85. Publish with `distros: debian-trixie`.

Ubuntu noble is not a factory target. It ships no gtk4-layer-shell, and its
newest rustc is 1.91.

## Gentoo target

`gentoo` renders the recipe into `gui-wm/tuna-desktop/tuna-desktop-<PV>.ebuild`
in the `tunaos` overlay (`1.0.0~beta.1` becomes PV `1.0.0_beta1`; see ADR
0002). The ebuild inherits `cargo.eclass` with `RUST_MIN_VER` from
`build.rust_version`, fetches the same tag archive and vendor asset, and runs
the recipe's build and install commands. Every dependency is stable on amd64
in `::gentoo`; the build takes them from Gentoo's binhost, and builds only
gtk4-layer-shell from source. A version bump needs no Gentoo-specific edit
beyond `build.rust_version` when the floor moves. Publish with
`publish-tideforge-gentoo.yml`, `packages: tuna-desktop`. A consumer adds the
binhost to `binrepos.conf` with `verify-signature = true` and lsigns
`public.gpg` in `/etc/portage/gnupg`:

```ini
# /etc/portage/binrepos.conf/tunaos.conf
[tunaos]
priority = 10
sync-uri = https://repo.tunaos.org/gentoo/binpkgs/amd64
verify-signature = true

# /etc/portage/repos.conf/tunaos.conf (only to rebuild from source)
[tunaos]
location = /var/db/repos/tunaos
sync-type = zipfile
sync-uri = https://repo.tunaos.org/gentoo/overlay/tunaos-overlay.zip
```

The served index contains `CPV: gui-wm/tuna-desktop-1.0.0_beta1` and
`PATH: gui-wm/tuna-desktop/tuna-desktop-1.0.0_beta1-1.gpkg.tar`.
Guppy's desktop stage uses `default/linux/amd64/23.0/desktop/systemd`,
`--binpkg-respect-use=y`, `*/* -abi_x86_32`, and an elogind mask. The
earlier `--binpkg-respect-use=n` setting applies to its base bootstrap,
not its desktop install. Keep the desktop policy: otherwise Gentoo's binhost
can select incompatible elogind/multilib dependency builds. This package's
only IUSE is cargo.eclass's `debug`, disabled in both profiles.

The edge blocks requests with `User-Agent: Python-urllib/*`.
For binary downloads, Portage supplies its own HTTP identification.
For overlay sync, Portage's `zipfile` module uses the generic agent.
The verifier supplies `User-Agent: TunaOS-Package-Verifier/1.0` to `emaint`.
Source-overlay consumers must also set an agent for HTTP requests.
The binhost does not need the overlay.

## Hummingbird

Tideforge does not build tuna-desktop for Hummingbird. Its `hummingbird`
target stays `scaffold`, and its publisher cannot write the build-chain prefix
safely (#832). The Hummingbird build chain builds it from the reviewed spec
`src/hummingbird/tuna-desktop/tuna-desktop.spec`. The root is the Fedora 44 +
public-hummingbird + utah-packages root, which also builds gtk4-layer-shell,
libseat and libinput. The `tuna` entry in `manifests/hummingbird-desktops.yaml`
declares the roots. Fedora does not ship tuna-desktop, so the generated
`build-order-hummingbird-desktops.yml` builds it in `local-00`, the last tier.

The script does not edit the spec. For each new tag, do these steps:

1. Set `upstream_version` to the tag without the `v`.
2. Set `Version:` to the `~` form and reset `Release:` to 1.
3. Add a `%changelog` entry.
4. Replace the two lines of `sources` with the SHA-512 values of the tag archive and of the vendor asset (`sha512sum`).

When the spec and the recipe have different versions,
`tests/test_hummingbird_recipe_sync.py` fails. To publish, run
`publish-build-chain-rpms.yml` with
`cells: hummingbird-x86_64,hummingbird-aarch64` and `tiers: local-00`.
The packages go to `hummingbird/20251124-{x86_64,aarch64}`.
