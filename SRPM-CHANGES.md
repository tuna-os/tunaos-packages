# Local SRPM Modification Changelog

This document tracks all manual modifications made to SRPM specifications and sources to enable GNOME 50 on CentOS Stream 10.

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
