# TunaOS Packages

`tunaos-packages` is TunaOS's source-controlled package repository. It owns
native packages, patches, build order, validation, signatures, and publication.
This lets TunaOS ship its desktop stacks independently of third-party repositories.

## Current state

The active production implementation is the native EL10 RPM build chain for
GNOME and XFWL4. Package specifications and EL10 compatibility fixes live in
`src/`; `build-order*.yml` declares the build order; GitHub Actions invokes
`scripts/build-chain.sh` in isolated Mock environments.

The project will move publication to GitHub Actions and Cloudflare R2. Any
remaining COPR projects are compatibility/bootstrap infrastructure, not a
desired end state. They will remain available until their GitHub/R2 replacement
has passed build, staged-install, and desktop runtime gates. Do not remove or
rewrite the native specs for GNOME on EL10 while that migration is incomplete.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the deployed RPM/R2
pipeline and [COPR-AUDIT.md](COPR-AUDIT.md) for the historical package-source
inventory.

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

The build chain uses native RPM tools for the GNOME bootstrap on EL10.
It supports RPM scriptlets, file triggers, SELinux policy and bootstrap variants.
It also supports dependency workarounds that a generic recipe format does not yet model.

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

1. Prefer the target distribution's package when it already meets TunaOS's
   version and integration requirements.
2. Add or update the native spec, patches, and source metadata under `src/`.
3. Place the package in the correct build-order manifest, after its build-time
   dependencies.
4. Build it in the declared Mock target and install it from the staged
   repository.
5. Add a focused runtime/desktop gate before promotion to users.

Useful local commands:

```bash
just --list
python3 scripts/parse-build-order.py build-order.yml --validate
./scripts/build-chain.sh --help
```

## Tideforge and cross-distro packaging

Tideforge is a single-recipe abstraction for straightforward packages across
RPM, DEB, and Pacman targets. [The package-factory contract](docs/PACKAGE_FACTORY.md)
defines supported targets, the upstream-source policy, and the migration away
from COPRs and PPAs.

It must prove source, build, install, and runtime parity before it replaces any
native packages for GNOME on EL10. Native specs remain the authoritative production
path for EL10-specific compatibility work until then.

## Release policy

Before promotion, maintainers must review the source and package definitions.
The target build must succeed. Packages from the staged repository must install
cleanly, and the relevant desktop/session validation must pass. Automated source
updates should open review PRs; they must never publish directly.

<!-- hive-contribute-plea: donated-compute appeal, keep in sync across repos -->
## Contribute compute — no code needed

No time to write code? You can still push this project's backlog forward. A TunaOS AI-agent hive works on this repository. Lend the hive your AI subscription or API tokens, and your machine runs tasks from this project's backlog.

- 🏫 [Contribute compute to the school hive](https://school.tunaos.org/contribute)
