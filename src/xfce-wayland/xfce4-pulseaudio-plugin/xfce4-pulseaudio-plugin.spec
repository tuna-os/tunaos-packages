Name: xfce4-pulseaudio-plugin
Version: 0.5.1
Release: 1%{?dist}
Summary: PulseAudio panel plugin for the Xfce desktop (Wayland)
License: GPL-2.0-or-later
URL: https://gitlab.xfce.org/panel-plugins/xfce4-pulseaudio-plugin
Source0: https://archive.xfce.org/src/panel-plugins/xfce4-pulseaudio-plugin/0.5/xfce4-pulseaudio-plugin-%{version}.tar.xz

BuildRequires: gcc
BuildRequires: meson
BuildRequires: ninja-build
BuildRequires: gtk3-devel
BuildRequires: libxfce4ui-devel
BuildRequires: libxfce4util-devel
BuildRequires: xfce4-panel-devel
BuildRequires: libxfce4windowing-devel
BuildRequires: exo-devel
BuildRequires: pulseaudio-libs-devel
BuildRequires: libnotify-devel
BuildRequires: gettext

Requires: xfce4-panel
# pulseaudio-daemon, not pulseaudio. CentOS Stream 10 ships no pulseaudio
# package at all: pipewire-pulseaudio OBSOLETES it and provides
# pulseaudio-daemon, pulseaudio-module-bluetooth and pulseaudio-module-jack --
# but NOT the bare `pulseaudio` name. Verified against CS10 AppStream
# metadata, where pipewire-pulseaudio is the sole provider of
# pulseaudio-daemon. So `Requires: pulseaudio` could never resolve, and the
# clean-install verify failed with "nothing provides pulseaudio needed by
# xfce4-pulseaudio-plugin" on BOTH arches (#482).
#
# This is what upstream Fedora's own spec requires, for the same reason.
Requires: pulseaudio-daemon
# The plugin's "Audio mixer..." action launches pavucontrol; without it that
# menu entry is dead. Upstream Fedora requires it too, and CS10 AppStream
# carries it, so matching upstream costs nothing here.
Requires: pavucontrol

%description
PulseAudio volume control panel plugin for the Xfce desktop environment
with Wayland support.

%prep
%autosetup -n xfce4-pulseaudio-plugin-%{version}

%build
# keybinder-3.0 is X11-only (global key grabs); the Wayland session binds
# the volume keys through xfce4-settings instead.
%meson -Dmixer-command=pavucontrol -Dkeybinder=disabled
%meson_build

%install
%meson_install
%find_lang %{name}

%files -f %{name}.lang
%license COPYING
%{_libdir}/xfce4/panel/plugins/libpulseaudio-plugin.so
%{_datadir}/xfce4/panel/plugins/*.desktop
%{_datadir}/icons/hicolor/*/apps/*.png
%{_datadir}/icons/hicolor/scalable/apps/*.svg
%{_datadir}/icons/hicolor/scalable/status/*.svg

%changelog
* Fri Nov 20 2026 James Reilly <jreilly1821@gmail.com> - 0.5.1-1
- Update to 0.5.1 (meson). Drop the libxfce4panel header-guard patch: this
  release includes the umbrella header itself.

* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - 0.4.10-1
- Initial XFCE Wayland package for TunaOS EL10
