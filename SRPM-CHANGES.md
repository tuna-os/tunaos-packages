# Local SRPM Modification Changelog

This document tracks all manual modifications made to SRPM specifications and sources to enable GNOME 50 and GNOME 51 on CentOS Stream 10.

## Orca 51 native accessibility predecessor

The changed-input native build in
`/tmp/gnome51-orca-native.5F9uN9/build.log` passed spec parsing and dependency
installation, then found AT-SPI 2.56.1 where Orca 51.0's Meson requires 2.58.6.
The native order had no AT-SPI source; only the separate Hummingbird order
referenced one.

Add native AT-SPI 2.58.6 in `src/gnome-51/at-spi2-core`, following the package
split and XWayland helper from Utah recipe revision
`76ed1d97fefd5a1cb90b240c596a1237a3819328`. Use the minimum stable upstream
release required by Orca, not Utah's development 2.61.1 payload. Its Python
override is included in the main RPM. The native graph builds it after GI and
before GTK/Orca. Orca now declares both AT-SPI pkg-config minimums and runtime
providers, as well as its upstream Meson/Python/PyGObject floors. No upstream
requirements were lowered. Native builds passed for AT-SPI in
`/tmp/gnome51-orca-native.5F9uN9/at-spi2-core-build.log` and Orca in
`/tmp/gnome51-orca-native.5F9uN9/orca-build.log`. Orca's desktop-file `%check`
ran; the AT-SPI spec has no `%check`. A real AlmaLinux package transaction
then exposed a missing Gdk 3.0 runtime provider. Add Orca's required Gtk3 and
PyGObject runtime dependencies, not just their build dependencies. The rebuilt
RPM installed and printed Orca 51.0 with AT-SPI2 2.58.6; its Python override
loaded. GLib and SELinux policy stayed unchanged. These results do not qualify
the full desktop or a live accessibility session.

## GNOME 51 native source-version macros

The bounded native Orca build recorded in
`/tmp/gnome51-orca-native.dMIgb1/build.log` failed before dependency resolution:
EL10's initial Mock root rejected `%gnome_check_version` as an unknown tag,
although the outer Fedora runner could create the SRPM.

Orca, gnome-session, gnome-settings-daemon, and libadwaita now derive their
source series and tarball names locally, following the existing Ptyxis spec.
Their `%prep` checks still reject `.alpha`, `.beta`, and `.rc` version spelling
and accept RPM's tilde spelling. No buildroot macro-package replacement or
version-check suppression is needed. Native dependency resolution and rebuild
remain required; this change does not establish runtime closure.

## GNOME 51.0 release set

The GNOME 51 tier moved from development snapshots to the coordinated
GNOME 51 stable release for issue #673. The core GNOME modules are at 51.0
(Nautilus starts the stable series at 51.0.1), with the matching GLib 2.90.0,
GTK 4.24.0, libadwaita 1.10.0, and GJS 1.90.0 releases. Both GLib bootstrap
and full specs move together; the full build retains a higher release so it
supersedes the bootstrap RPM.

The libadwaita 1.10.0 tarball includes the stylesheet/sassc fix previously
carried as `fix-sassc-requirement-for-tarball-builds.patch`, so that downstream
patch was removed. All other downstream patches were checked against the final
release tarballs and still apply.

gsettings-desktop-schemas 51.rc removed the GConf schema migration files
(`/usr/share/GConf/gsettings/*.convert`), so the 51.0 spec no longer lists
them in `%files`.

## 1. Custom GNOME 50 Compatibility Packages

### `gnome50-el10-compat`
*   **Origin**: New package.
*   **Purpose**: Restores upstream `systemd-user` PAM behavior to fix dynamic GDM greeter user authentication.
*   **Modifications**:
    *   Created `systemd-user.pam` with `account required pam_permit.so`.
    *   Spec file provides override at `/etc/pam.d/systemd-user`.

## 2. Modified Rawhide Backports

### `gnome-shell` (GNOME 50 and 51, issue #747)

* **Cause**: the main RPM provided `gnome-shell-common` while `%files common`
  owned the GSettings XML. DNF satisfied the dependency without the real common
  RPM. Yellowfin's GDM then crashed with a missing `org.gnome.shell` schema.
* **Change**: remove the false provider and obsolete rule. Retain the exact
  dependency on the common RPM. Increase releases to `50.0-4` and `51~beta-2`.
* **Evidence**: the published `50.0-3` RPM headers and file lists confirm the
  split. Installing only the matching common RPM restored the schema in a
  chroot of a separate VM disk; composefs still hid the change on boot.
  CentOS Stream 10 parsed both new specs with the correct
  dependency and no self-provider. See [the experiment record](docs/gnome-shell-common-2026-09-26.md).

### `gi-docgen`
*   **Origin**: Backport from Rawhide.
*   **Modifications**:
    *   Patched spec to remove `BuildSystem: pyproject` (unsupported on EL10).
    *   Fixed `%autosetup` directory name mismatch (`gi_docgen` vs `gi-docgen`).
    *   Switched to local SRPM build in COPR.

### `gnome-user-share`
*   **Origin**: Backport from Rawhide.
*   **Modifications**:
    *   Generated and added a **vendor tarball** (`gnome-user-share-48.1-vendor.tar.xz`) to bypass missing Rust crate dependencies in EL10 repositories.
    *   Patched spec to use the vendor tarball and configure offline cargo build.
    *   Added `BuildRequires: pkgconfig(glib-2.0)` which was missing in the Rawhide spec.

### `gdm`
*   **Origin**: Backport from Rawhide.
*   **Modifications**:
    *   Added `Requires: gnome50-el10-compat` to ensure the PAM workaround is automatically installed during a GNOME 50 upgrade.

### `glib2`
*   **Origin**: Backport from Rawhide.
*   **Modifications**:
    *   Retained EL10-specific schema compilation triggers and `gio` module query triggers.

### `gdk-pixbuf2`
*   **Origin**: Custom / Backport from Rawhide.
*   **Modifications**:
    *   Added `%bcond glycin` support to gate glycin BuildRequires, Requires, and `-Dglycin` meson setup flag.
    *   Added `gdk-pixbuf2-bootstrap.spec` with glycin disabled to break the `gdk-pixbuf2 -> glycin -> gtk4 -> gdk-pixbuf2` build cycle.


### `gnome-shell` (GNOME 49)
*   **Origin**: Backport from F43 Dist-Git (`src/gnome-49/gnome-shell/`).
*   **Modifications**:
    *   Removed `revert-gir-2.0-port.patch` — this patch forced gnome-shell to use the old `g_irepository_*` API (libgirepository-1.0) but gjs 1.86 links libgirepository-2.0; loading both caused `GIRepository` GType double-registration → crash.
    *   Removed `0001-Revert-Require-gjs-1.81.2-for-build-because-Intl.Seg.patch` — obsolete now that COPR has gjs 1.86.0.
    *   Added `BuildRequires: pkgconfig(girepository-2.0)` and updated `glib2_version` to 2.86.0.
    *   Updated `0001-el10-stub-gnome-qr.patch` to stub out `js/ui/qrCode.js` in addition to removing the eager import from `js/misc/dependencies.js`. The full crash chain is `authPrompt.js → webLogin.js → qrCode.js → import GnomeQR`, which is a static import chain that fires at startup. The stub exports a no-op `QrCode` widget (St.Bin subclass) so the import chain resolves without GnomeQR typelib.


*The following were built as local SRPMs to resolve dependency conflicts or provide newer versions than what COPR's `distgit` fetcher was pulling.*

*   **`libgexiv2`**: Backported to version 0.16.0 to resolve Nautilus dependencies.
*   **`pango` (GNOME 49)**: Added `BuildRequires: python3-setuptools` to fix `ModuleNotFoundError: No module named distutils` in `g-ir-scanner` during build. Bumped release to 2.
*   **`tinysparql` (GNOME 49)**: Added `BuildRequires: python3-setuptools` to fix `ModuleNotFoundError: No module named distutils` in `g-ir-scanner` during build. Bumped release to 2.
*   **`libgexiv2` (GNOME 49)**: Added `BuildRequires: python3-setuptools` to fix `ModuleNotFoundError: No module named distutils` in `g-ir-scanner` during build. Bumped release to 3.
*   **`libcloudproviders` (GNOME 49)**: Added `BuildRequires: python3-setuptools` to fix `ModuleNotFoundError: No module named distutils` in `g-ir-scanner` during build. Bumped release to 3.
*   **`tinysparql`**: Backported to version 3.11~rc to resolve GNOME 50 dependencies.
*   **`localsearch`**: Backported to version 3.11~rc to resolve GNOME 50 dependencies.
*   **`libical`**: Rebuilt against ICU 77 to fix `evolution-data-server` dependency chain.
*   **`evolution-data-server`**: Rebuilt against ICU 77.
*   **`libebur128`**: Backported from Rawhide (dependency for Pipewire 1.6).
*   **`libzip`**: Backported from Rawhide (dependency for Localsearch).
*   **`glycin`**: Built from local SRPM to resolve `gdk-pixbuf2` obsolescence conflicts in Rawhide spec.
