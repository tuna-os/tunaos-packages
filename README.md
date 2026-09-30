# TunaOS Packages

`tunaos-packages` is TunaOS's source-controlled package repository.
It manages specs for packages, patches, build order, validation, signatures, and publication.
This repo ships curated desktop stacks without third-party repositories.

## Current state

The active production implementation is the native EL10 RPM build chain for GNOME and XFWL4.
Package specifications and EL10 compatibility fixes live in `src/`.
Manifests in `build-order*.yml` declare the build order.
GitHub Actions runs `scripts/build-chain.sh` in isolated Mock environments.

The project now moves publication to GitHub Actions and Cloudflare R2.
Any remaining COPR projects are compatibility infrastructure.
They remain available until their GitHub and R2 replacements pass build, staged-install, and runtime gates.
Do not remove or rewrite the native GNOME specs for EL10 while that work is incomplete.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the now deployed RPM/R2 pipeline and [COPR-AUDIT.md](COPR-AUDIT.md) for the historical package inventory.

## How a package moves through the repository

```text
upstream source + native packaging/patches
        ↓
build-order manifest
        ↓
GitHub Actions + Mock build environment
        ↓
repository install and desktop/runtime validation
        ↓
sign and publish to the TunaOS repository
```

The build chain stays native for the EL10 GNOME bootstrap.
It supports RPM scriptlets, file triggers, SELinux policy, and bootstrap variants.
It also handles dependency workarounds that a generic format does not yet model.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/gnome-50/`, `src/deps/` | Native EL10 GNOME RPM specs, patches, and source metadata |
| `src/xfce-wayland/` | Native XFCE/XFWL4 RPM packaging |
| `build-order.yml` | GNOME 50 bootstrap/build dependency order |
| `build-order-xfce*.yml` | XFCE/XFWL4 build order for EL10 and Fedora |
| `scripts/build-chain.sh` | Shared local/CI RPM build engine |
| `.github/workflows/` | Per-package, distributed, validation, signing, and publication workflows |
| `manifests/hummingbird-desktops.yaml` | Fedora Hummingbird desktop RPM catalog |

## Adding or changing a package

1. Prefer the target distribution package when it meets version and integration requirements.
2. Add or update the native spec, patches, and source metadata under `src/`.
3. Place the package in the correct build-order manifest, after its build-time dependencies.
4. Build it in the declared Mock target and install it from the staged repository.
5. Add a focused runtime test before promotion to users.

Useful local commands:

```bash
just --list
python3 scripts/parse-build-order.py build-order.yml --validate
./scripts/build-chain.sh --help
```

## Tideforge and cross-distro packaging

Tideforge is a single-recipe abstraction for packages across RPM, DEB, and Pacman targets.
The contract in [docs/PACKAGE_FACTORY.md](docs/PACKAGE_FACTORY.md) sets the supported targets, the policy for upstream sources, and migration plans.

It must prove parity in source, build, install, and runtime before it replaces native GNOME specs for EL10.
Native specs remain the authoritative production path for EL10 compatibility work until then.

## Release policy

Maintainers promote packages only after review of source code and specs.
Promotion also needs successful target builds, clean repository installs, and successful desktop tests.
Automated source updates open review PRs and never publish directly.
