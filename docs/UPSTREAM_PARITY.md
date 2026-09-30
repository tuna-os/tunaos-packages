# Upstream parity register

TunaOS ships desktop experiences curated by the Bluefin, Aurora, and Zirconium communities.
Parity means maintainers carry forward selected applications, defaults, session behavior, hardware integration, and update experience.
It does not mean the installation of similarly named programs.
This is a compatibility target, not permission to use external binary repositories.
The target distribution must provide each item below, TunaOS must build it, or maintainers must mark it out of scope.

The audit inspected the revisions of upstream repositories on 2026-07-25:

| Upstream | Revision | Scope |
| --- | --- | --- |
| [Zirconium](https://github.com/zirconium-dev/zirconium) | `c43b53abfb75296f8517990823fd2cc9f095d837` | Niri/DMS desktop and session integration |
| [Bluefin](https://github.com/ublue-os/bluefin) | `742b3b77b924aeff47610b6c985dd1805dd5e927` | GNOME workstation utilities and update experience |
| [Aurora](https://github.com/ublue-os/aurora) | `6dd632ebefd04bacc4eba0319f3106e739417cc0` | KDE workstation utilities and update experience |

## Rules

1. A TunaOS image must not enable a COPR, PPA, or upstream binary repository.
2. Fedora, EPEL, CentOS Stream, Ubuntu, and Debian packages remain preferred when their version meets the experience requirement.
3. When upstream lacks a package, TunaOS imports source with a pinned checksum, license review, native specs, and target gates.
4. Maintainers keep curated configuration in TunaOS with upstream attribution. They never copy files opaquely from upstream images; they import reviewed files as source, test them, and maintain them here.

## Initial parity inventory

| Experience | Current upstream-only gap | Factory disposition | Gate before promotion |
| --- | --- | --- | --- |
| Niri compositor | `niri` currently comes from `yalter/niri-git` | Source RPM now builds and clean-installs on EL10 with Tideforge `libseat` and upstream session/portal assets; retain user-session validation before promotion | Ubuntu/Debian install; Wayland session smoke test |
| DMS shell | `quickshell-git`, `dms`, `dms-cli`, `dms-greeter` come from AvengeMedia COPRs | Source recipes are present. Quickshell and the full DMS/greetd closure build and clean-install on EL10 from staged Tideforge RPMs; retain user-session/login validation before promotion | greetd login and DMS user-session smoke test |
| COSMIC session | COSMIC's session manager, compositor, portal, settings, and greeter use vendor archives plus nonstandard install contracts | Tideforge models pinned sources, offline Cargo vendor configurations, native install contracts, and the two EL10 greeter patches for the complete root set. The icon themes, session manager, background manager, compositor, idle manager, notification daemon, on-screen display service, panel/dock, settings app, settings daemon, and display tool have real EL10 builds and staged-install proof; their generated RPM payloads are CI-gated. The remaining COSMIC runtime packages must still be staged as a complete release set before promotion. | COSMIC login/session smoke |
| Niri sensors | Zirconium's `iio-niri` needs hardware-specific validation | Source RPM builds and clean-installs on EL10 against the Tideforge Niri stack; do not promote until its rotation behavior is tested | accelerometer service and rotation smoke test on hardware-capable runner |
| Niri companion tools | Zirconium includes `dgop`, `dsearch` (now upstream `danksearch`), and `dankcalendar-git`; legacy TunaOS also references `valent-git` | `dgop` and `danksearch` build and clean-install on EL10 from source. Dank Calendar now has an Arch Tideforge source recipe using the upstream tagged archive with vendored modules and bundled DankCommon; EL10/DEB intake waits for a source-built Go 1.26 toolchain. A fresh EL10 repository probe confirms that `valent` is not stock content, so it remains an optional source-intake candidate rather than a target dependency. | package install plus application/service smoke test |
| Greeter integration | Zirconium owns greetd PAM, sysusers, tmpfiles, DMS policy, and session files; TunaOS currently copies portions from its image | Import the curated configuration with attribution into TunaOS and package generated helpers; priority 0 | `greetd` service, PAM, and Niri login e2e test |
| XFCE login | XFCE Wayland uses `gtkgreet` hosted by Cage for its graphical greetd login | Tideforge owns pinned `gtk-layer-shell` and `gtkgreet` recipes. `gtk-layer-shell` has an EL10 staged-install gate; `gtkgreet` promotion is gated by consuming its generated `-devel` RPM. Retain the native spec meanwhile. | `greetd` + Cage + `gtkgreet` login smoke |
| XFCE compositor | `xfwl4` needs a Cargo vendor tree and XFCE Wayland protocol submodule not present in the upstream archive | Tideforge now models both checksum-locked source inputs, including the exact offline Cargo replacement configuration. Native RPM remains the promoted path until the full Xfce prerequisite closure is staged and runtime-tested. | Xfce Wayland nested/TTY session smoke |
| Bluefin updates/store | `uupd` is the remaining Bluefin COPR package; Bazaar is version-dependent upstream/Fedora content | `uupd` has a factory recipe. Bazaar now has a pinned Tideforge source recipe for Ubuntu 26.04, Debian sid, and Arch; its EL10 enablement waits for the staged GNOME 50 library set because upstream requires GTK 4.22 and libadwaita 1.8. | update command contract and Flatpak-store launch test |
| Aurora KDE add-ons | `krunner-bazaar`, `oversteer-udev`, `kairpods`, `sunshine`, and Aurora's patched `plasma-setup` | Split into independently licensed source packages; do not import Aurora's COPR binaries. The EL10 probe confirms `sunshine` and `plasma-setup` are absent from stock repositories. Plasma Setup now has a pinned upstream KDE source recipe for the current Arch Plasma stack; EL10 waits for its latest-KDE staging repository. Sunshine needs a separate toolchain/bootstrap review before recipe intake. | EL10/KDE install and feature-specific runtime tests |
| Aurora SELinux workaround | Aurora's `ublue-os-selinux-workarounds` mitigates a Linux 7.0 composefs/overlay execmem regression | Do not ship on EL10: its source policy explicitly targets Linux 7.0 and grants `kernel_t` execmem; retain an evidence-based re-evaluation if the target kernel acquires that defect | Not applicable unless an EL10 reproducer exists |

The long package lists for Fedora from Bluefin and Aurora are not factory inputs.
TunaOS compares these lists continuously.
It uses distribution packages where available.
If we rebuild a Fedora package to duplicate it, maintenance costs rise without parity gains.

## Snapshot audit (#226)

`_upstream-snapshots/` holds the package declarations this register tracks.
It contains one YAML per upstream (`bluefin-lts`, `aurora`, `zirconium`).
These files key to the upstream revisions above and focus on curated packages.
Each declaration carries the intended disposition: a source recipe, a desktop declaration, a named distribution package, or an out-of-scope entry with a reason.
`scripts/audit-upstream-parity.py` verifies the repository honours it:

```
scripts/audit-upstream-parity.py --strict
scripts/audit-upstream-parity.py --report-json docs/upstream-parity-report.json
```

Any snapshot package without a disposition fails `--strict`.
A snapshot package also fails if the repository does not honor the declared recipe or reason.
`tests/test_upstream_parity_audit.py` asserts that committed snapshots stay covered.

The register also recommends a check of installed package lists in published images.
That check needs container registry access and belongs in the image pipeline.
This repository provides the recipe side of the contract (rules 1–4 and the inventory table).

## Delivery order

1. Replace the Niri/DMS COPR chain with source packages and TunaOS configuration.
2. Replace the RPM dependencies for COSMIC and GNOME in the factory plan.
3. Close gaps for Bluefin `uupd` and Aurora KDE add-ons.
4. Add a CI job to verify package availability across distributions and prevent unvetted external repositories.

An intake entry is not a release promise.
It gains support only after source provenance, license, native builds, staged installs, and desktop tests pass.
