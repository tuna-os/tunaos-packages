Name: xfce4-power-manager
Version: 4.21.1
Release: 2%{?dist}
Summary: Power manager for the Xfce desktop environment (Wayland)
License: GPL-2.0-or-later
URL: https://gitlab.xfce.org/xfce/xfce4-power-manager
Source0: https://archive.xfce.org/src/xfce/xfce4-power-manager/4.21/xfce4-power-manager-%{version}.tar.xz

BuildRequires: gcc
BuildRequires: gtk3-devel
BuildRequires: libxfce4ui-devel
BuildRequires: libxfce4util-devel
BuildRequires: libxfce4windowing-devel
BuildRequires: xfconf-devel
BuildRequires: xfce4-panel-devel
BuildRequires: upower-devel
BuildRequires: polkit-devel
BuildRequires: libnotify-devel
BuildRequires: wayland-devel
BuildRequires: wayland-protocols-devel
BuildRequires: wlr-protocols
BuildRequires: intltool
BuildRequires: gettext
BuildRequires: meson
BuildRequires: ninja-build

Requires: upower
Requires: polkit
Requires: libxfce4windowing

%description
Power manager for the Xfce desktop environment with Wayland support.

%prep
%autosetup -n xfce4-power-manager-%{version}

%build
%meson -Dwayland=enabled
%meson_build

%install
%meson_install
%find_lang %{name}

%files -f %{name}.lang
%license COPYING
%{_bindir}/xfce4-power-manager
%{_bindir}/xfce4-power-manager-settings
%{_sbindir}/xfpm-power-backlight-helper
%{_sbindir}/xfce4-pm-helper
%{_libdir}/xfce4/panel/plugins/libxfce4powermanager.so
%{_datadir}/xfce4/panel/plugins/*.desktop
%{_datadir}/applications/*.desktop
%{_sysconfdir}/xdg/autostart/*.desktop
%{_datadir}/polkit-1/actions/*.policy
%{_datadir}/man/man1/*
%{_datadir}/icons/hicolor/*/apps/*.png
%{_datadir}/icons/hicolor/*/status/*.png
%{_datadir}/icons/hicolor/*/status/*.svg
%{_datadir}/icons/hicolor/scalable/apps/*.svg
%{_datadir}/metainfo/*.xml

%changelog
* Fri Nov 20 2026 James Reilly <jreilly1821@gmail.com> - 4.21.1-2
- Build from the 4.21.1 release tarball instead of the same commit's git archive (XFCE 4.21 tag set shared with tuna-os/brussels)

* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - 4.21.1-1
- Initial XFCE Wayland package for TunaOS EL10
