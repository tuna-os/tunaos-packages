# Tideforge switch-over readiness

Assessed 2026-07-27 against `feat/universal-package-recipes` (#115) with the fixes
from #125 applied.

Per `README.md`, Tideforge *"must prove source, build, install, and runtime
parity before it replaces native packages on EL10."* The promotion contract in
`docs/PACKAGE_FACTORY.md` is stricter still. Every candidate must *"build in the target
buildroot and pass package tests. It must install from the staged repository and pass a
runtime smoke test when the package affects a session."*

> **Addendum, 2026-09-02.** The strategy question this note kept open is
> settled in `docs/PACKAGE_FACTORY.md` ("One contract, one build engine per target").

> Tideforge stays the recipe renderer for targets with complete coverage (COSMIC and Niri on EL10 and DEB; the Niri/DMS stack on Tumbleweed and Arch). Each engine builds in its own root and shares the common contract.

> We measured and rejected a BuildStream-style "build once, package at the end" model for compiled code (`docs/experiments/tideforge-universal-intermediate.md`). TunaOS consumes GNOME on Hummingbird from utah-packages instead of local builds (`docs/HUMMINGBIRD-TARGET.md` §7-8). GNOME on DEB is its own measured chain (`gnome51-deb.yaml`), not a Tideforge rollout step. The runtime-gate table below is still the open work.

## Verdict

**The switch as usually framed -- "move TunaOS packages to Tideforge" -- is not
available.** Tideforge is not immature. But for the EL10 GNOME stack that
TunaOS ships, Tideforge is not a candidate. The manifest declares
`implementation: native-spec`. There is nothing to switch.

What is available today is a partial switch of two stacks -- **COSMIC and
niri** -- whose recipe coverage is complete. CI cannot promote these packages under the
current contract. The runtime gates that the contract needs do not exist yet.

| Parity dimension | State | Evidence |
| --- | --- | --- |
| Source | **Proven** | `verify-tideforge-source.py` runs per package in CI; every recipe pins URL + SHA-256 |
| Build | **Proven** (after #125) | All 40 recipes render for every declared target, 0 failures |
| Install | **Partial** | 8 `Clean-install` jobs out of 40 recipes |
| Runtime | **Not started** | 0 of 12 declared gate types implemented |

## Recipe coverage per queue root

40 recipes exist. Coverage of the roots each queue declares:

| Queue | Target | Implementation | Roots | Have recipe | Missing |
| --- | --- | --- | ---: | ---: | --- |
| cosmic | el10 | native-spec | 14 | **14** | — |
| cosmic | ubuntu | tideforge-debian | 14 | **14** | — |
| cosmic | debian | tideforge-debian | 14 | **14** | — |
| gnome | el10 | native-spec | *(build_order)* | — | *Tideforge not proposed* |
| gnome | ubuntu | tideforge-debian | 9 | **1** | glib2, gobject-introspection, gtk4, libadwaita, mutter, gnome-shell, gnome-session, gdm |
| gnome | debian | tideforge-debian | 9 | **1** | *(same 8)* |
| kde | el10 | native-spec | 3 | **3** | — |
| kde | arch | tideforge-pkgbuild | 4 | **4** | — |
| niri | el10 | native-spec | 9 | **9** | — |
| niri | ubuntu | tideforge-debian | 8 | **8** | — |
| niri | debian | tideforge-debian | 8 | **8** | — |
| niri | opensuse-tumbleweed | tideforge-rpm | 8 | **8** | — |
| niri | arch | tideforge-pkgbuild | 9 | **9** | — |
| xfce | el10 | native-spec | 4 | 3 | xfce4-wayland |
| xfce | fedora | native-spec | 3 | 2 | libxfce4ui |
| xfce | debian | tideforge-debian | 3 | 2 | libxfce4ui |
| xfce | arch | native-pkgbuild | 3 | 2 | libxfce4ui |

**Read the row for GNOME on DEB twice.** Both queues for GNOME on DEB name Tideforge
and declare 9 roots, but only `bazaar` has a recipe. The eight missing packages form the entire
platform stack for GNOME -- glib2 through gdm. That is not a gap to close incrementally; it is
the hard part of the problem.

## Declared gates vs. implemented gates

Counted across all five queue manifests:

| Gate | Times declared | Implemented? |
| --- | ---: | --- |
| container-build | 12 | **yes** |
| rpm-md-stage-install | 7 | partial — 8 `Clean-install` jobs total |
| apt-stage-install | 7 | partial — same |
| greetd-login | 7 | **no** |
| mock-build | 5 | **yes** |
| niri-session-smoke | 5 | **no** |
| xfce-wayland-session-smoke | 4 | **no** |
| cosmic-session-smoke | 3 | **no** |
| gnome-session-smoke | 3 | **no** |
| pacman-stage-install | 3 | partial |
| plasma-session-smoke | 2 | **no** |
| selinux-enforcing | 1 | **no** |

**The project declares every runtime and session gate, but builds none.** The manifests describe
a promotion contract that CI cannot evaluate now. Until that changes, no Tideforge
recipe is contract-legal for promotion regardless of how green the build matrix is.

For contrast, the native GNOME path on EL10 has runtime verification.
`build-gnome50-verify.yml` boots a CentOS Stream 10 VM under Lima, waits for GDM, checks
that gnome-shell survives without crashes, and scans the journal for crash
signatures. That is the standard Tideforge must meet.

## COSMIC installs and smokes, but the full staged closure is still ahead

The gate cell for cosmic-session (unified factory, #430) installs the package into a
clean container and runs its smoke contract. `start-cosmic` and the
desktop file for wayland sessions are present. But CI cannot stage the desktop end-to-end. The remaining runtime closure (greetd-selinux,
adw-gtk3-theme, and the nine cosmic siblings that cosmic-session needs) is not
all factory-built yet.

So COSMIC meets its install and smoke gate per package. But it does not meet the
full staged-desktop gates (`rpm-md-stage-install`, `greetd-login`, or
`cosmic-session-smoke` on a complete session) that the desktop edition needs.

## Other gaps found

* **The manifest declares aarch64, but CI never builds it.** `manifests/package-factory.yaml` declares el10
  `architectures: [x86_64, aarch64]` and the same for ubuntu/debian (`amd64, arm64`).
  Tideforge CI builds x86_64/amd64 only. Any promotion would ship a half-architecture repo.
* **niri on Arch does not build.** `ld.lld: undefined symbol: spa_format_parse_libspa_rs`
  and other errors appear -- a pipewire-rs/libspa version skew against current Arch pipewire. This
  is a genuine upstream build problem, not a package or renderer bug.
* **The build did not honour `build_repositories`.** The manifest declares el10
  `build_repositories: [crb, epel]`, but the `rpm-payload` job enabled only CRB. This
  caused 13 of the 18 build failures fixed in #125. Nothing
  now checks that a buildroot of a workflow matches the contract that it claims to follow.

## Recommended sequence

1. **Do not** try to move GNOME/EL10 to Tideforge. It uses native specs by design and the
   README policy keeps it authoritative. Treat this as settled.
2. **Build the session gates first**, using the Lima+VNC approach in `build-gnome50-verify.yml`.
   Start with `greetd-login` -- it is the most-declared runtime gate (7x)
   and gates both COSMIC and niri.
3. **Then promote niri**, not COSMIC. niri has full recipe coverage across four targets and
   the smallest runtime closure. COSMIC waits on a runtime closure that is not yet
   factory-built.
4. **Add aarch64** to the Tideforge matrix before any promotion, or narrow the declared
   architectures in the manifest to match reality.
5. **Treat GNOME on DEB as a separate project**, not a Tideforge rollout step. Eight
   platform packages (glib2, gtk4, mutter, gnome-shell, gdm and friends) represent a large
   effort comparable to the native EL10 work.

## What #125 does and does not prove

#125 fixes all 18 `Build Tideforge supported targets` failures. That proves the **build**
dimension across the covered recipes, and it removes the noise that was hiding the real
readiness picture. It does **not** advance source, install, or runtime readiness, because the
gates that would test those are not built. Read a green matrix on #115 as
"the renderer works", not "Tideforge is ready to take over."
