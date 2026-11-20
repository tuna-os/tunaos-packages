Name:           xfce4-panel
Version:        4.21.3
Release:        1%{?dist}
Summary:        Panel for the Xfce desktop environment (Wayland-ready)

License:        GPL-2.0-or-later AND LGPL-2.1-or-later
URL:            https://gitlab.xfce.org/xfce/xfce4-panel
Source0: https://archive.xfce.org/src/xfce/xfce4-panel/4.21/xfce4-panel-%{version}.tar.xz

BuildRequires: gcc
BuildRequires:  gtk3-devel
BuildRequires:  libxfce4ui-devel
BuildRequires:  libxfce4util-devel
BuildRequires:  libxfce4windowing-devel
# libxfce4session-client-1 >= 4.21.2
BuildRequires:  xfce4-session-devel >= 4.21.2
BuildRequires:  garcon-devel
BuildRequires:  gobject-introspection-devel
BuildRequires:  vala
BuildRequires:  intltool
BuildRequires:  gettext
BuildRequires:  meson
BuildRequires:  ninja-build
BuildRequires:  gtk-layer-shell-devel
BuildRequires:  libdbusmenu-gtk3-devel

Requires:       libxfce4ui
Requires:       libxfce4windowing
Requires:       garcon
Requires:       gtk-layer-shell

%description
The Xfce4 panel is the taskbar/launcher for the Xfce desktop environment.
This build targets Wayland compositors via gtk-layer-shell and supports
the xfce4-panel 4.21.x development series.

%package devel
Summary: Development files for %{name}
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
Headers, pkgconfig, GObject-Introspection typelib, and Vala bindings for
building xfce4-panel plugins.

%prep
%autosetup -n xfce4-panel-%{version}

%build
# Wayland-only build (matches thunar's -Dx11=disabled convention in this
# stack) — avoids pulling in libwnck3/libX11/libXext just for the X11 backend.
%meson -Dx11=disabled
%meson_build

%install
%meson_install
%find_lang %{name}

%files -f %{name}.lang
%license COPYING
%{_bindir}/xfce4-panel
%{_bindir}/xfce4-popup-applicationsmenu
%{_bindir}/xfce4-popup-directorymenu
%{_bindir}/xfce4-popup-windowmenu
%{_libdir}/libxfce4panel*.so.*
%{_libdir}/xfce4/panel/
%{_datadir}/applications/*.desktop
%{_datadir}/xfce4/panel/
%{_datadir}/icons/hicolor/*/apps/org.xfce.panel*.png
%{_datadir}/icons/hicolor/*/apps/org.xfce.panel*.svg
%{_sysconfdir}/xdg/xfce4/panel/default.xml

%files devel
%{_includedir}/xfce4/libxfce4panel-2.0/
%{_libdir}/libxfce4panel*.so
%{_libdir}/pkgconfig/libxfce4panel-2.0.pc
%{_libdir}/girepository-1.0/*.typelib
%{_datadir}/gir-1.0/*.gir
%{_datadir}/vala/vapi/*

%changelog
* Fri Nov 20 2026 James Reilly <jreilly1821@gmail.com> - 4.21.3-1
- Update to the 4.21.3 release tarball (XFCE 4.21 tag set shared with tuna-os/brussels)

* Thu Aug 20 2026 TunaOS Bot <bot@tunaos.org> - 4.21.0-2
- Drop the XfceTitledDialog autoptr shim that collides with libxfce4ui 4.21

* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - 4.21.0-1
- Initial XFCE Wayland package for TunaOS EL10
