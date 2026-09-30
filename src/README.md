# src/ — Package Source Trees

This directory contains RPM spec files and patches, organized into three
trees that serve different build targets.

## Directory Layout

```
src/
├── deps/           Shared dependency packages (55 package dirs)
├── gnome-49/       GNOME 49 stack (42 package dirs)
├── gnome-50/       GNOME 50 stack (21 package dirs)
├── hummingbird/    COSMIC desktop packages
└── xfce-wayland/   XFCE Wayland packages
```

## The Three GNOME Trees

### Why three directories exist

The GNOME 49 and GNOME 50 stacks track **different upstream releases**
(Fedora F43 dist-git and Rawhide dist-git, respectively). Each needs its own
set of spec files because:

- **Version differences**: GNOME 49 ships e.g. glib2 2.86.x, mutter 47.x;
  GNOME 50 ships glib2 2.88.x, mutter 48.x.
- **Build-environment differences**: Each version has its own bootstrap
  sequence (glib2 ↔ gobject-introspection circular dependency) and its own
  set of downstream patches for EL10 compatibility.
- **Independent pipelines**: GNOME 49 builds via COPR
  (`jreilly1821/c10s-gnome-49`) + self-hosted GHA; GNOME 50 builds via the
  main `build-order.yml` GHA pipeline. They publish to separate R2 paths.

### `deps/` — shared dependency pool

`deps/` is the **shared pool** of packages that are not version-specific
to one GNOME release. These packages are consumed by:

| Build target | Build-order file | deps/ refs |
|---|---|---|
| GNOME 50 (main) | `build-order.yml` | 35 packages |
| GNOME 49 (COPR) | `.copr/build-order-gnome49.yml` | 4 packages |
| Hummingbird desktops | `build-order-hummingbird-desktops.yml` | 35 packages |
| fprintd | `build-order-fprintd.yml` | 2 packages |
| fprintd (aarch64) | `build-order-fprintd-aarch64.yml` | 2 packages |

### `gnome-49/` — GNOME 49 version-specific packages

Contains upstream-version-specific packages for the GNOME 49 build.
All 38 packages in the GNOME 49 build-order come from this tree, plus
4 shared deps from `deps/`.

### `gnome-50/` — GNOME 50 version-specific packages

Contains upstream-version-specific packages for the GNOME 50 build.
The main `build-order.yml` uses 18 packages from this tree alongside
35 from `deps/`.

## Known Duplication

Some packages appear in **two or three** of these directories. This is
tracked in [Issue #52](https://github.com/tuna-os/tunaos-packages/issues/52).

### Three-way duplicates (deps + gnome-49 + gnome-50)

| Package | deps/ version | gnome-49 version | gnome-50 version |
|---|---|---|---|
| gtk4 | 4.21.6 | 4.20.3 | 4.22.1 |
| vte291 | — | — | — |
| gnome-online-accounts | (unwired) | — | — |

### Two-way: deps/ ↔ gnome-49/ (16 packages)

avahi, colord-gtk, fontconfig, gnome-online-accounts, gsound, gtk4,
harfbuzz, libei, libgexiv2, localsearch, meson, mod_dnssd, pango,
tecla, tinysparql, vte291

### Two-way: gnome-49/ ↔ gnome-50/ (20 packages)

These are **intentionally versioned** — different upstream GNOME releases.

gdm, gjs, glib2, gnome-control-center, gnome-desktop3,
gnome-initial-setup, gnome-online-accounts, gnome-session,
gnome-settings-daemon, gnome-shell, gobject-introspection,
gsettings-desktop-schemas, gtk4, libadwaita, mutter, nautilus,
ptyxis, vte291, xdg-desktop-portal, xdg-desktop-portal-gnome

### Unwired packages in deps/

The following 6 `deps/` packages are **not referenced** by any build-order
manifest. They may be legacy imports, reference material, or pre-built
sources:

el10-v2-buildflags, freetype, gnome-online-accounts, icu, lzo, mozjs128

## Build Orders

Build orders are tiered YAML manifests that define package build
sequencing. They live at the repo root:

| File | Target | Primary source trees |
|---|---|---|
| `build-order.yml` | GNOME 50 on CS10 | deps/ + gnome-50/ |
| `build-order-hummingbird-desktops.yml` | COSMIC desktop | deps/ + gnome-50/ + hummingbird/ |
| `build-order-fprintd.yml` | fprintd (x86_64) | deps/ |
| `build-order-fprintd-aarch64.yml` | fprintd (aarch64) | deps/ |
| `build-order-xfce.yml` | XFCE Wayland | xfce-wayland/ |
| `build-order-xfce-fedora.yml` | XFCE on Fedora | xfce-wayland/ |
| `.copr/build-order-gnome49.yml` | GNOME 49 on CS10 (COPR) | gnome-49/ + deps/ (4) |

## How Packages Map to R2

```
R2 bucket (bluefin)
├── repo/10/x86_64/              ← GNOME 50 + deps (build-order.yml)
├── gnome49/10-stream-x86_64/    ← GNOME 49 (.copr/build-order-gnome49.yml)
└── ...
```

The Cloudflare Worker at `repo.tunaos.org` serves all paths.
