# Porting Wayland XFCE to every TunaOS base

TunaOS must not ship X11 on any image.
Today XFCE uses X11 on all targets except EL10.
This document surveys what each ecosystem needs, measured against real base images.

## The finding

> **CORRECTION (measured, 2026-07-19).**
> An earlier version of this document claimed every distro needs one package.
> That was incorrect, because it assumed package presence without a test build.
> A real build on Fedora 44 fails:
>
> ```text
> Package dependency requirement 'libxfce4kbd-private-3 >= 4.21.4'
>   could not be satisfied.
> Package 'libxfce4kbd-private-3' has version '4.20.2',
>   required version is '>= 4.21.4'
> ```
>
> **xfwl4 4.21.0 needs the XFCE 4.21 libraries. Every base except Gentoo ships 4.20.x.**
> The corrected position is below.

**Only Gentoo needs nothing.
Everywhere else needs the XFCE 4.21 core libraries and `xfwl4`** -- which matches what EL10 builds.

XFCE 4.20 added upstream Wayland support, and that part of the survey holds.
Every base ships 4.20 or newer.
Their `xfce4-panel` links `libgtk-layer-shell` and `libwayland-client`.
All bases provide the greeter stack (`greetd`, `gtkgreet`, and `cage`).
We measured those facts and they stay unchanged.

xfwl4 does not build against 4.20.
The compositor tracks the 4.21 development series.
Its `-sys` crates need `>= 4.21.4` via `pkg-config`, so 4.20.x libraries fail before compilation.
Package presence does not prove version adequacy.
Only a real build confirms compatibility.

EL10 is the outlier because it ships no XFCE packages.
`build-order-xfce.yml` builds the whole stack for EL10.
On other distributions, duplicate packages create a maintenance burden.
`xfwl4` is the primary gap because only Gentoo and AUR maintain packages for this Rust compositor.

## Survey

Measured from the base images in `.github/build-config.yml` in the `tunaOS` repo.

The `libxfce4ui` version decides the work, because `xfwl4` needs `>= 4.21.4`:

| Base | libxfce4ui | Meets xfwl4 >= 4.21.4 | Consequence |
|---|---|---|---|
| Gentoo | **4.21.9** | ✅ | nothing to build -- xfwl4 also in tree |
| Fedora 44 | 4.20.2 | ❌ | needs 4.21 core libs + xfwl4 |
| Debian trixie | 4.20.1 | ❌ | needs 4.21 core libs + xfwl4 |
| Ubuntu resolute | 4.20.2 | ❌ | needs 4.21 core libs + xfwl4 |
| Arch / openSUSE | 4.20.x | ❌ | needs 4.21 core libs + xfwl4 |
| EL10 | (ours) | ✅ | already builds the whole stack |

Measurements come from real builds on Fedora, `apt-cache policy libxfce4ui-2-dev` on Debian and Ubuntu, and Gentoo package databases.

### What "4.21 core libs" means -- measured on Fedora

A test build on Fedora 44 needs **three packages** to satisfy crate probes:

| Crate | Probe requirement | Fedora ships | We build |
|---|---|---|---|
| `xfconf-sys` | `libxfconf-0 >= 4.21.2` | 4.20.0 | **xfconf 4.21.2** |
| `libxfce4kbd-private-sys` | `libxfce4kbd-private-3 >= 4.21.4` | 4.20.2 | **libxfce4ui 4.21.7** |
| — | — | — | **xfwl4 4.21.0** |

Result: `xfwl4-4.21.0-1.fc44.x86_64.rpm`, `EXIT=0`.

We omit two packages from that set on purpose:

- **`libxfce4util`** -- our spec builds 4.20.1, which is older than Fedora's 4.20.2.
  Including it tries a downgrade.
- **`libxfce4windowing`** -- Fedora's 4.20.4 satisfies every probe, so a custom build is unnecessary.

EL10 builds the full stack because EL10 ships none of it.
A distribution with half the stack needs only components below the version floor.
Developers cannot copy the package list from EL10.
They must derive the set for each distribution through builds.

Those packages carry native names from the distribution.
When TunaOS ships these packages, it delivers a newer XFCE core than the upstream distribution.
Package managers treat that as an upgrade (4.21.9 > 4.20.2).
TunaOS maintains those libraries on that base until the distribution updates to 4.21 or 4.22.

Maintainers must decide whether to provide the 4.21 core or wait for the distribution.

### Original per-base capability survey (unchanged, still accurate)

| Base | Variant(s) | XFCE | panel is Wayland-capable | greetd | gtkgreet | cage | rust | **xfwl4** |
|---|---|---|---|---|---|---|---|---|
| Fedora 44 | bonito, bonito-rawhide | 4.20.3/4.20.7 | ✅ | 0.10.3 | 0.8 | 0.3.1 | 1.96 | ❌ **build** |
| Debian trixie | flounder | 4.20.2/4.20.4 | ✅ | 0.10.3 | 0.8 | 0.2.0 | 1.85 | ❌ **build** |
| Debian sid | flounder-sid | ≥ trixie | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ **build** |
| Ubuntu resolute | grouper | 4.20.4/4.20.7 | ✅ | 0.10.3 | 0.8 | 0.2.1 | 1.93 | ❌ **build** |
| Arch | marlin | 4.20.4/4.20.7 | ✅ | 0.10.3 | `greetd-gtkgreet` 0.8 | 0.3.1 | 1.97 | ⚠️ AUR `xfwl4-git` |
| openSUSE TW | sailfin | 4.20.4/4.20.7 | ✅ | 0.10.3 | 0.8 | 0.3.1 | 1.97 | ❌ **build** |
| Gentoo | guppy | 4.21.2 | ✅ | in tree | 0.8 | in tree | ✅ | ✅ **`xfce-base/xfwl4` 4.21.0** |
| EL10 | yellowfin, albacore, skipjack | ✗ none | — | COPR | packaged here | base | ✅ | ✅ packaged here |

"panel is Wayland-capable" means `xfce4-panel` depends on `gtk-layer-shell` and `wayland-client`:

- Debian: `apt-cache depends xfce4-panel` -> `libgtk-layer-shell0`, `libwayland-client0`
- Arch: `pacman -Si xfce4-panel` -> `gtk-layer-shell`
- openSUSE: `zypper info --requires` -> `libgtk-layer-shell.so.0`, `libwayland-client.so.0`
- Fedora: `dnf repoquery --requires` -> same two

## Work per ecosystem

Ordered by priority.
Exposure to X11 today affects **bonito (+rawhide)** and **flounder**.
Variants `grouper`, `marlin`, `flounder-sid`, and `sailfin` are experimental in `tunaOS#641`.
They do not deliver X11 to end users now.

### 1. Gentoo (guppy) -- zero package changes

`xfce-base/xfwl4` 4.21.0 exists in the official Portage tree.
Its `keywords` field is empty, so it needs an entry in `package.accept_keywords` to install.
That needs a manifest update in the `tunaOS` repo.

guppy has no XFCE flavor today (base/gnome/kde).
This step adds a new flavor to the image.

### 2. Fedora (bonito, bonito-rawhide) -- reuse existing RPM

`build-order-xfce-fedora.yml` and `mock/fedora-44-ci.cfg` exist.
The chain needs one package and one tier.
The work waits on `build-xfce-distributed.yml`, which now uses runners for CentOS Stream 10.

### 3. openSUSE (sailfin) -- reuse RPM with adjusted macros

The build uses the same spec file with different RPM macros.
Differences include `BuildRequires` names, license tags, and `%{?rhel}` conditionals.
Scaffold location: `packaging/opensuse/`.

### 4. Debian + Ubuntu (flounder, flounder-sid, grouper) -- one deb source

All three targets share a single source package.
Scaffold location: `packaging/debian/`.

### 5. Arch (marlin) -- PKGBUILD

AUR `xfwl4-git` exists and tracks git commits.
TunaOS packages a version pinned to the commit used by RPM and DEB builds.
This ensures that every variant delivers the identical compositor.

A test build with `makepkg` confirmed the three-package set on Arch.
The build needed no fourth package:

- `xfconf` 4.21.2-1 -- `makedepends` required `glib2-devel` for `glib-genmarshal`.
- `libxfce4ui` 4.21.7-1 -- applied the same `glib2-devel` dependency.
- `xfwl4` 4.21.0-1 -- built cleanly after installation of the two packages above.

Arch packages `libxfce4util` with `.vapi` files, so Vala support remains enabled.
`makepkg` allows network access during build, so `cargo fetch` pulls the Smithay dependency directly.

## What makes xfwl4 harder than a normal Rust package

Constraints from `xfwl4.spec` apply to every ecosystem:

1. **`resources/xfce-wayland-protocols` is a git submodule.**
   It contains custom protocol XML and is absent from release archives.
   The build accesses it by relative path at `resources/xfce-wayland-protocols/`.

2. **Cargo dependencies need a vendor archive.**
   `Cargo.toml` pulls Smithay from git.
   Without a network connection, cargo cannot connect to git.
   The build uses a vendor archive and a Cargo configuration that redirects crates.io and git sources.

3. **Upstream includes the udev cfg gate as of commit 5c2802c.**
   The old patch is no longer required.
   `wlr_screencopy.rs` gates `dma` behind `#[cfg(any(feature = "udev", feature = "winit"))]`.

4. **Feature flags need manual settings.**
   Build with `--no-default-features --features udev,egl,xwayland,smithay/renderer_pixman,smithay/renderer_gl`.

5. **The build needs `GETTEXT_SYSTEM=1`.**
   Without it, `gettext-sys` compiles GNU gettext and fails on `libintl_gettext`.
   On glibc targets, libc includes gettext.

6. **The build sets `RUSTFLAGS="-C relocation-model=pic"`.**

## Status

Skeletons in `packaging/` are scaffolds, not verified builds.
They record constraints and package names.
Every skeleton needs a real build test before production use.
