Name: xfce4-pulseaudio-plugin
Version: 0.4.9
Release: 1%{?dist}
Summary: PulseAudio panel plugin for the Xfce desktop (Wayland)
License: GPL-2.0-or-later
URL: https://gitlab.xfce.org/panel-plugins/xfce4-pulseaudio-plugin
Source0: https://archive.xfce.org/src/panel-plugins/xfce4-pulseaudio-plugin/0.4/xfce4-pulseaudio-plugin-0.4.9.tar.bz2
# Three source files include libxfce4panel/xfce-panel-plugin.h directly
# instead of the umbrella libxfce4panel.h — newer libxfce4panel enforces
# "only include the umbrella header" and errors out.
Patch0: 0001-fix-libxfce4panel-header-guard.patch

BuildRequires: gcc
BuildRequires: gtk3-devel
BuildRequires: libxfce4ui-devel
BuildRequires: libxfce4util-devel
BuildRequires: xfce4-panel-devel
BuildRequires: libxfce4windowing-devel
BuildRequires: exo-devel
BuildRequires: pulseaudio-libs-devel
BuildRequires: libnotify-devel
BuildRequires: intltool
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
with Wayland support. This version (0.4.9) predates the meson-based
build system used elsewhere in this stack — it's autotools
(configure/make).

%prep
%autosetup -n xfce4-pulseaudio-plugin-%{version} -p1

%build
%configure
%make_build

%install
%make_install
%find_lang %{name}

%files -f %{name}.lang
%license COPYING
%{_libdir}/xfce4/panel/plugins/libpulseaudio-plugin.so
%{_datadir}/xfce4/panel/plugins/*.desktop
%{_datadir}/icons/hicolor/48x48/apps/*.png
%{_datadir}/icons/hicolor/scalable/apps/*.svg
%{_datadir}/icons/hicolor/scalable/status/*.svg

%changelog
* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - 0.4.10-1
- Initial XFCE Wayland package for TunaOS EL10
