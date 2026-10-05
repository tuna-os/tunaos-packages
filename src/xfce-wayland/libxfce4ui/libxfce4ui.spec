%global commit 5ce63c5114cf5cf5501bfde2def98e91e9bb128c
%global shortcommit %(c=%{commit}; echo ${c:0:7})
Name: libxfce4ui
Version: 4.21.7
Release: 70%{?dist}
Summary: Common UI library for Xfce
License: LGPL-2.0-or-later
URL: https://gitlab.xfce.org/xfce/libxfce4ui
Source0: https://gitlab.xfce.org/xfce/libxfce4ui/-/archive/%{commit}/libxfce4ui-%{commit}.tar.gz
BuildRequires: gcc, meson, ninja-build
BuildRequires: gtk3-devel, libxfce4util-devel, xfconf-devel
BuildRequires: libSM-devel
BuildRequires: startup-notification-devel
BuildRequires: libgtop2-devel, libepoxy-devel, libgudev-devel
BuildRequires: gobject-introspection-devel, vala, intltool, gettext
# Same as xfconf: meson.build does find_program('xdt-gen-visibility',
# required: true), and that script ships in xfce4-dev-tools.
BuildRequires: xfce4-dev-tools
%description
Common UI library for Xfce desktop environment.

%package devel
Summary: Development files for %{name}
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
Headers, pkgconfig, GObject-Introspection typelib, and Vala bindings for
building against libxfce4ui.

%prep
%autosetup -n libxfce4ui-%{commit}

# This commit has fully migrated to meson (no configure.ac/autogen.sh).
%build
%meson
%meson_build

%install
%meson_install
%find_lang %{name}

%files -f %{name}.lang
%license COPYING
%{_libdir}/libxfce4ui*.so.*
%{_libdir}/libxfce4kbd-private*.so.*
%{_bindir}/xfce-desktop-item-edit
%{_bindir}/xfce-open
%{_bindir}/xfce4-about
%config(noreplace) %{_sysconfdir}/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-keyboard-shortcuts.xml
%{_datadir}/applications/xfce4-about.desktop
%{_datadir}/icons/hicolor/*/apps/*.png
%{_datadir}/icons/hicolor/*/apps/*.svg
%{_datadir}/pixmaps/libxfce4ui/

%files devel
%{_includedir}/xfce4/libxfce4ui-2/
%{_includedir}/xfce4/libxfce4kbd-private-3/
%{_libdir}/libxfce4ui*.so
%{_libdir}/libxfce4kbd-private*.so
%{_libdir}/pkgconfig/*.pc
%{_libdir}/girepository-1.0/*.typelib
%{_datadir}/gir-1.0/*.gir
%{_datadir}/vala/vapi/*

%changelog
* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - %{version}-%{release}
- Packaged for the XFCE Wayland stack (tuna-os/tunaos-packages#65)

