# Desktop Parity Audit & Contract Specifications

This document describes requirements for desktop parity and verification tools for [#133](https://github.com/tuna-os/tunaos-packages/issues/133).

## Context & Problem Statement

An audit of 37 published images showed that **24 editions lacked packages for the desktop**.

- **`marlin` non-GNOME editions (`marlin:kde`, `marlin:cosmic`, `marlin:niri`, `marlin:xfce`)**: No-op builds produced images without session files or desktop packages.
- **`flounder` cosmic/niri editions (`flounder:cosmic`, `flounder:niri`)**: Smaller than `flounder:base` and missing session components.
- **`sailfin`, `flounder`, and `grouper`**: Names of RPM packages failed without errors under `apt` and `zypper`, which led to incomplete installs.

The size of an image cannot distinguish missing packages from differences in desktop architecture.

## Desktop Experience Contracts & Validation

To ensure desktop completeness, package contracts and verification tools check all target desktops across all base distributions (RPM, DEB, openSUSE).

### 1. GNOME Desktop Contract

Defined in [`docs/gnome-desktop-contract.md`](./gnome-desktop-contract.md) and checked via [`scripts/verify-gnome-desktop-experience.py`](https://github.com/tuna-os/tunaos-packages/blob/main/scripts/verify-gnome-desktop-experience.py).
Required package components:

- `gdm`
- `gnome-keyring`
- `gnome-session`
- `gnome-shell`
- `gvfs`
- `mutter`
- `nautilus`
- `xdg-desktop-portal-gnome`

### 2. Contract Enforcement Rules for All Desktops

1. **Hard Failure on Unresolved Names**: Image builds on Debian/Ubuntu (`apt`), openSUSE (`zypper`), and EL/Fedora (`dnf`) must fail on unresolved package names.
2. **Inventory of Installed Packages**: Every build must export its installed package list (`rpm -qa` / `dpkg-query -W`) to make parity comparable.
3. **Session Verification**: Every desktop edition must ship session files in `/usr/share/wayland-sessions/` or `/usr/share/xsessions/`, greeters, and portals.

## Status & Action Plan

1. **`marlin` non-GNOME and `flounder` cosmic/niri**: Tests enforce verification and fail builds when session files are missing.
2. **Package Name Mapping**: CI audits mappings across DNF, APT, and ZYPPER definitions in `manifests/`.
