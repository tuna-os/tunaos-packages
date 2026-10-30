# Tuna Desktop for Hummingbird (tuna-os/tunaos-packages#832).
#
# Built by the Hummingbird build chain (Fedora 44 + public-hummingbird +
# utah-packages + this target's own published index), in the same root as
# the gtk4-layer-shell, libseat and libinput it links, and published with
# them by publish-build-chain-rpms.yml. packages/tuna-desktop/package.yaml
# is the same package for every other target: version, upstream tag,
# dependency floors, build commands and installed files follow it, and
# tests/test_hummingbird_recipe_sync.py fails when the two disagree on the
# release. Bump both together (docs/packaging/tuna-desktop-release.md).
%bcond check 1

# The release binaries are what upstream ships and what every other target
# packages; no debuginfo subpackage, as in the recipe.
%global debug_package %{nil}
# The upstream tag minus its v. RPM's Version writes the prerelease `-` as `~`
# so it sorts before the final release.
%global upstream_version 1.0.0-beta.2

Name:           tuna-desktop
Version:        1.0.0~beta.2
Release:        2%{?dist}
Summary:        Tuna Desktop: GNOME-style Wayland desktop session in Rust (v1 beta)
License:        GPL-3.0-or-later
URL:            https://github.com/tuna-os/tuna-desktop
Source0:        %{url}/archive/refs/tags/v%{upstream_version}.tar.gz#/tuna-desktop-%{upstream_version}.tar.gz
# Vendored crates.io closure of exactly this tag (cargo vendor --locked
# --versioned-dirs), published by scripts/prepare-tuna-desktop-release.py so
# the offline buildroot never fetches crates.
Source1:        https://github.com/tuna-os/tunaos-packages/releases/download/tuna-desktop-vendor-v%{upstream_version}/tuna-desktop-vendor.tar.gz

# The recipe's fedora BuildRequires. The floors: GTK 4.12 (shell-gtk's
# v4_12), gtk4-layer-shell 1.1 (gtk4-session-lock-sys v1_1) and Rust 1.92,
# the highest rust-version in the tag's Cargo.lock. clang-devel is bindgen's
# libclang (pipewire/libspa).
BuildRequires:  cargo >= 1.92
BuildRequires:  rust >= 1.92
BuildRequires:  gcc
BuildRequires:  clang-devel
BuildRequires:  pkgconf-pkg-config
BuildRequires:  pkgconfig(dbus-1)
BuildRequires:  pkgconfig(glib-2.0)
BuildRequires:  pkgconfig(gdk-pixbuf-2.0)
BuildRequires:  pkgconfig(gtk4) >= 4.12
BuildRequires:  pkgconfig(gtk4-layer-shell-0) >= 1.1
BuildRequires:  pkgconfig(libadwaita-1)
BuildRequires:  pkgconfig(libdrm)
BuildRequires:  pkgconfig(libinput)
BuildRequires:  pkgconfig(libsecret-1)
BuildRequires:  pkgconfig(xkbcommon)
BuildRequires:  pkgconfig(xkbcommon-x11)
BuildRequires:  pkgconfig(gbm)
BuildRequires:  pkgconfig(egl)
BuildRequires:  pkgconfig(libpipewire-0.3)
BuildRequires:  pkgconfig(libseat)
BuildRequires:  pkgconfig(libsystemd)
BuildRequires:  pkgconfig(libudev)
BuildRequires:  pkgconfig(wayland-client)
BuildRequires:  pkgconfig(wayland-server)

# RPM derives the ELF library requirements itself. These keep the version
# floors and what it cannot see: libpam is dlopen()ed by the lock screen,
# libsecret is reached over D-Bus, dbus and pipewire are services, and the
# settings-daemon plugins, session targets, SVG loader and sound theme are
# started or loaded at run time.
Requires:       dbus
Requires:       glib2
Requires:       gnome-settings-daemon
Requires:       gnome-session
Requires:       gdk-pixbuf2
Requires:       librsvg2
Requires:       fontconfig
Requires:       libcanberra
Requires:       sound-theme-freedesktop
Requires:       gtk4 >= 4.12
Requires:       gtk4-layer-shell >= 1.1
Requires:       libadwaita
Requires:       libdrm
Requires:       libinput
Requires:       libsecret
Requires:       libxkbcommon
Requires:       libxkbcommon-x11
Requires:       mesa-libEGL
Requires:       mesa-libgbm
Requires:       pipewire
Requires:       libseat
Requires:       systemd-libs
Requires:       libwayland-client
Requires:       libwayland-server
Requires:       pam
# The former name (tuna-os/tuna-desktop#505), as on every other target.
Provides:       roost

%description
Tuna Desktop (formerly Roost) is a Smithay-based Wayland desktop that aims
at GNOME 51 parity: a supervised compositor with a GNOME-style GTK shell,
optional scrollable tiling and an IBus bridge; login is through GDM. This
package ships the six session binaries, the wayland-sessions entry, the
lock-screen PAM service, the session target, the Lockdown portal backend and
a private cursor fallback, plus the retained session and PAM bridge for the
former `roost` name.

%prep
%autosetup -n tuna-desktop-%{upstream_version}
tar --extract --file %{SOURCE1}
mkdir -p .cargo
cat > .cargo/config.toml <<'EOF'
[source.crates-io]
replace-with = "vendored-sources"

[source.vendored-sources]
directory = "vendor"

[net]
offline = true
EOF

%build
# Every binary reports this from --version; read at compile time.
export TUNA_VERSION=v%{upstream_version}
# The pipewire crate compiles C through cc (libspa-sys), whose LTO bitcode
# loses its symbols at the Rust link; fat LTO objects keep real code beside it.
export CFLAGS="${CFLAGS:-} -ffat-lto-objects"
export CARGO_HOME="$PWD/.cargo-home"
cargo build --release --locked --offline -p tuna-compositor -p tuna-wallpaper-svg -p tuna-shell-host -p tuna-shell-gtk

%install
for b in compositor session shell-host shell-gtk ibus-bridge wallpaper-svg; do
    install -Dm0755 target/release/tuna-$b %{buildroot}%{_bindir}/tuna-$b
done
# Private GNOME 51 scalable cursor fallback.
install -d %{buildroot}%{_datadir}/tuna-desktop/icons
cp -a share/tuna-desktop/icons/. %{buildroot}%{_datadir}/tuna-desktop/icons/
# The shell's Lockdown portal backend and the session's portal preferences.
install -Dm0644 share/xdg-desktop-portal/portals/tuna.portal %{buildroot}%{_datadir}/xdg-desktop-portal/portals/tuna.portal
install -Dm0644 packaging/marlin/tuna-portals.conf %{buildroot}%{_datadir}/xdg-desktop-portal/tuna-portals.conf
install -Dm0644 share/wayland-sessions/tuna.desktop %{buildroot}%{_datadir}/wayland-sessions/tuna.desktop
install -Dm0644 share/systemd/user/tuna-session.target %{buildroot}%{_prefix}/lib/systemd/user/tuna-session.target
# Lock-screen PAM service (tuna-desktop#62): the compositor's unlock.
install -Dm0644 share/pam.d/tuna-lock %{buildroot}%{_sysconfdir}/pam.d/tuna-lock
# Retained legacy bridge (tuna-desktop#780): the hidden roost.desktop keeps a
# session GDM remembers as "roost" starting Tuna Desktop, and roost-lock
# serves a session started before the upgrade. The roost-* binary links went
# with tuna-desktop#775.
install -Dm0644 share/compat/wayland-sessions/roost.desktop %{buildroot}%{_datadir}/wayland-sessions/roost.desktop
install -Dm0644 share/compat/pam.d/roost-lock %{buildroot}%{_sysconfdir}/pam.d/roost-lock

%if %{with check}
%check
# The recipe's verify smoke, against the built tree: every binary reports
# the upstream tag minus its v, and the session entries launch the
# supervisor.
for b in compositor session shell-host shell-gtk ibus-bridge wallpaper-svg; do
    v="$(%{buildroot}%{_bindir}/tuna-$b --version)"
    test "${v##* }" = "%{upstream_version}" || { echo "tuna-$b --version reports '$v'" >&2; exit 1; }
done
grep -qx 'Exec=tuna-session' %{buildroot}%{_datadir}/wayland-sessions/tuna.desktop
grep -qx 'DesktopNames=Tuna;GNOME;' %{buildroot}%{_datadir}/wayland-sessions/tuna.desktop
grep -qx 'Exec=tuna-session' %{buildroot}%{_datadir}/wayland-sessions/roost.desktop
grep -qx 'NoDisplay=true' %{buildroot}%{_datadir}/wayland-sessions/roost.desktop
%endif

%files
%license LICENSE
%doc docs/nested-session.md
%{_bindir}/tuna-compositor
%{_bindir}/tuna-session
%{_bindir}/tuna-shell-host
%{_bindir}/tuna-shell-gtk
%{_bindir}/tuna-ibus-bridge
%{_bindir}/tuna-wallpaper-svg
%{_datadir}/tuna-desktop/icons
%{_datadir}/xdg-desktop-portal/portals/tuna.portal
%{_datadir}/xdg-desktop-portal/tuna-portals.conf
%{_datadir}/wayland-sessions/tuna.desktop
%{_datadir}/wayland-sessions/roost.desktop
%{_prefix}/lib/systemd/user/tuna-session.target
%config(noreplace) %{_sysconfdir}/pam.d/tuna-lock
%config(noreplace) %{_sysconfdir}/pam.d/roost-lock

%changelog
* Fri Oct 30 2026 TunaOS Package Factory <packages@tunaos.org> - 1.0.0~beta.2-2
- Rebuild without tuna-greeter (tuna-os/tunaos-packages#950)
* Thu Oct 29 2026 TunaOS Package Factory <packages@tunaos.org> - 1.0.0~beta.2-1
- Tuna Desktop v1.0.0-beta.2 for Hummingbird
* Fri Oct 16 2026 TunaOS Package Factory <packages@tunaos.org> - 1.0.0~beta.1-1
- Tuna Desktop v1.0.0-beta.1 for Hummingbird (tuna-os/tunaos-packages#832)
