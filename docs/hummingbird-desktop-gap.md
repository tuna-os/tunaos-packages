# Hummingbird repository package contents

> **2026-09-02 addendum.**
> Item 1 below ("Hummingbird is Fedora Rawhide") set the build root for a month.
> Hummingbird rebuilds at Rawhide versions, but its ABI (glibc, openssl, python, perl) matches Fedora 44.
> Its build root is Fedora 44 plus its Pulp repository.
> Three packages from this factory used Rawhide `GLIBC_2.44` and fail on the target.
> The measured table, the corrected root, and index resolutions appear in [HUMMINGBIRD-TARGET.md](HUMMINGBIRD-TARGET.md).
> Gap counts stay unchanged: gnome needs 301 sources against Rawhide and 311 against Fedora 44.

Measured 2026-08-06 from a live rpm-md index.
Run the measurement script to reproduce the data:

```bash
scripts/measure-hummingbird-gap.py \
  --report-json docs/hummingbird-desktop-gap.json \
  --build-order build-order-hummingbird-desktops.yml
```

The file `docs/hummingbird-desktop-gap.json` contains machine-readable package lists, cycles, and index checksums.

## Why this was measured

The `LUKS hummingbird:gnome` workflow fails on `tuna-os/tunaOS` (run 31100096864, job 92611346523).
The image builds an artifact tagged `gnome` that contains no GNOME binaries.
The desktop contract rejects the image because it lacks `gnome-shell`, session files, and display managers.

The failure log records this sequence:

1. `Chroot not found in the given Copr project (hummingbird-20251124-x86_64)`
2. `TUNAOS_COPR_ENABLE_FAILED repo=jreilly1821/c10s-gnome-50-fresh`
3. `dnf versionlock` reports `No package found` for GNOME desktop packages.

Step 3 shows that the base repositories contain no desktop packages.
This document analyzes the gap.

## Index provenance

| index | revision | primary.xml sha256 | entries |
|---|---|---|---|
| `public-hummingbird/x86_64` | 1786016019 | `b92541eaf43fd4a8976710fa0035ae0c69b38aae7a0f241d61d87cd9bdd2a512` | 3384 binary package names, 16506 (name, evr, arch) tuples |
| Fedora Rawhide `Everything/x86_64/os` | 1785994410 | `dc3c7ec50508105e880c74f0178bf4723e770c73766654d833d1b2ad541e3774` | 66629 binary packages |
| Fedora Rawhide `Everything/source/tree` | 1785994313 | `87746b33eb94ca586ebc3d9407fc5c57ca6e8d9db8c899bc43aaa4fcd8ab5318` | 23178 SRPMs (used for BuildRequires ordering) |

Checksums matched on all three indexes.

## Item 1 -- Hummingbird tracks Fedora Rawhide, not EL10 or Fedora 43

Build scripts check the names of images for the string `fedora`.
`quay.io/hummingbird-community/bootc-os:latest` does not match, so the build selects EL10 COPR paths.
The index comparison shows this is incorrect:

| package | hummingbird | Fedora 43 | Fedora Rawhide (45) |
|---|---|---|---|
| glib2 | 2.89.3-1.hum1 | 2.86.5-1 | 2.89.3-1.fc45 |
| systemd | 261.2-1.hum1 | 258.10-1 | 261.2-1.fc45 |
| gcc | 16.1.1-2.hum1 | 15.3.1-1 | 16.1.1-4.fc45.1 |
| harfbuzz | 14.3.0-1.hum1 | — | 14.3.0-1.fc45 |
| qt6-qtbase | 6.11.1-5.hum1 | — | 6.11.1-5.fc45 |
| rust | 1.97.1-2.hum1 | — | 1.97.1-2.fc45 |
| meson | 1.11.2-2.hum1 | — | 1.11.2-2.fc45 |
| rpm | 6.0.1-6.hum1 | — | 6.0.92-2.fc45 |

Of 38 shared core packages, **30 match Rawhide versions and releases**.
Every package in Hummingbird uses the `.hum1` release tag.
Hummingbird is a rebuild of Fedora Rawhide.

## Item 2 -- Changing `IS_FEDORA` does not resolve the gap

Queries to Hummingbird repositories still fail to find desktop packages:

```text
gnome-shell   no      gtk4       no      cairo    no      pipewire   no
mutter        no      libadwaita no      wayland  no      wireplumber no
gdm           no      pango      no      mesa     no      dconf      no
nautilus      no      gvfs       no      libdrm   no      polkit     no
gsettings-desktop-schemas no      xdg-desktop-portal no   at-spi2-core no
```

Of 58 GNOME packages in the Fedora manifest, Hummingbird ships only `avahi`.
Index scans confirm no packages for `wayland`, `mesa`, `gtk4`, `cairo`, or `pipewire`.

The image SBOM confirms that Hummingbird provides a base operating system without desktop packages.

## Item 3 -- Fedora Rawhide binaries cannot be used directly

The factory cannot use Rawhide binaries due to ABI mismatches:

| package | hummingbird | Rawhide | consequence |
|---|---|---|---|
| glibc | 2.43-8.hum1 | 2.44-1.fc45 | 637 Rawhide binaries need `GLIBC_2.44`, which Hummingbird does not provide |
| libxml2 | 2.15.3 (`libxml2.so.16`) | 2.13.9 (`libxml2.so.2`) | soname mismatch in both directions |
| openssl | 3.5.6 (`libcrypto.so.3`) | 4.0.1 (`libcrypto.so.4`) | soname mismatch |
| python3 | 3.14.6 | 3.15.0~b4 | different `libpython3.x.so.1.0` |
| fontconfig | 2.17.1 | 2.18.2 | compatibility break |

In the GNOME closure alone, 20 packages need `libxml2.so.2` and 5 need `GLIBC_2.44`.
These differences cause install failures for packages.
The factory must build desktop packages in the Hummingbird build environment.

## Item 4 -- Size of the gap

Roots include each desktop's install packages and requirements.
The closure checks the graph of dependencies against Rawhide until Hummingbird satisfies each capability.

| desktop | roots | already in target | binaries to build | **source packages to build** | tiers |
|---|---|---|---|---|---|
| gnome | 58 | 1 (`avahi`) | 405 | **298** | 10 |
| kde | 13 | 0 | 459 | **384** | 19 |
| cosmic | 22 | 0 | 201 | **163** | 10 |
| niri | 26 | 0 | 369 | **310** | 10 |
| xfce | 15 | 0 | 316 | **248** | 12 |
| **union** | | | | **670** | |

Two packages (`dms-greeter` and `xfwl4`) do not exist in Fedora dist-git and use upstream source archives.
The gap represents the entire graphical stack, including `mesa`, `pipewire`, and `gtk4`.

## Item 5 -- Build order and bootstrap cycles

Tiers follow a topological order of the `BuildRequires` graph from Fedora source indexes.
A sequence based on runtime dependencies produces an incorrect single tier.

GNOME has a cyclic build dependency across 60 packages:

```text
ModemManager NetworkManager at-spi2-core bluez cairo colord flite gcr
gdk-pixbuf2 geoclue2 geocode-glib glib-networking glycin gnome-desktop3
gnome-settings-daemon gobject-introspection graphene gsettings-desktop-schemas
gssdp gstreamer1 gstreamer1-plugins-bad-free gstreamer1-plugins-base gtk3 gtk4
gupnp gupnp-igd gweather-locations json-glib libadwaita libcanberra
libcloudproviders libdecor libepoxy libgudev libgusb libgweather libical
libinput libmbim libnice libnotify libpcap libproxy libqmi libqrtr-glib
librsvg2 libsecret libsndfile libsoup3 libwacom mpg123 mutter pango pipewire
polkit ppp pulseaudio pygobject3 sbc upower xorg-x11-server-Xwayland
```

The build order marks these packages with `bootstrap: true`.
They need bootstrap specs or an initial pass with fewer features.

## Item 6 -- Python ABI dependencies

Measured 2026-08-08 after Python package failures in tier `niri-00`.
Hummingbird uses Python 3.14, while Rawhide uses Python 3.15:

| | hummingbird | Fedora Rawhide (45) | Fedora 44 |
|---|---|---|---|
| python3 | 3.14.6-2.2.hum1 | 3.15.0~rc1-1.fc45 | 3.14.6-1.fc44 |

Rawhide binaries need `python(abi) = 3.15` and cannot enter the Hummingbird buildroot.
Hummingbird lacks PEP 517 build backends such as `flit-core`, `wheel`, and `setuptools_scm`.
When `%pyproject_buildrequires` requests build backends, dnf5 fails to resolve them from Rawhide.

Build backends do not appear in runtime closures.
Across the 670 source packages, 60 build backends need Python 3.14.
A complete build of these backends from source needs 640 packages.

To resolve this without a Python rebuild, `mock/hummingbird-ci.cfg` pins Fedora 44 at priority 50.
It restricts inclusion to `python3-*` and `flit`.
This allows 3.14 build tools to satisfy build rules without exposure to newer C libraries.
The configuration removes this pin when Hummingbird updates to Python 3.15.

## Item 7 -- Base image component audit (#228)

The base image `quay.io/hummingbird-community/bootc-os:latest` includes 262 packages.
It provides `NetworkManager` but lacks packages for desktop integration.
`manifests/hummingbird-desktops.yaml` records components for each desktop:

| desktop | audited components | coverage |
|---|---|---|
| gnome | `NetworkManager-openvpn-gnome`, `NetworkManager-openconnect-gnome`, `NetworkManager-wwan`, `gnome-keyring`, `nautilus`, `xdg-desktop-portal-gnome`, `xdg-desktop-portal-gtk` | all in `install_packages` |
| kde | `xdg-desktop-portal-kde` | in `install_packages` |
| niri | `NetworkManager-tui`, `blueman`, `brightnessctl`, `playerctl`, `pavucontrol`, `gnome-keyring`, `nautilus`, `xdg-desktop-portal-gnome`, `xdg-desktop-portal-gtk`, `SwayNotificationCenter`, `waybar`, `fuzzel` | all in `install_packages` |
| xfce | `greetd`, `gtkgreet`, `cage`, `xdg-desktop-portal-gtk` | `greetd`/`gtkgreet`/`cage` added |

`greetd`, `gtkgreet`, and `cage` provide the Wayland greeter for XFCE.
COPR packages remain excluded from images and build as project recipes.

## What was not verified

- **No RPM was built during this audit:**
  To build 670 source packages needs a build farm.
  The R2 target serves no repodata now.
- **No evaluation of optional packages:**
  The tool ignores `Recommends` dependencies.
- **No evaluation of boolean rules:**
  We omit boolean rules.
- **The audit covers x86_64:**
  Measurements do not evaluate indexes for aarch64.
- **Item 6 is index arithmetic:**
  The test confirms packages in Fedora 44 indexes without a full build.
