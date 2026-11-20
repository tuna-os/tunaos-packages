%global _hardened_build 1

Name:           libxfce4util
Version:        4.21.0
Release:        1%{?dist}
Summary:        Basic utility library for Xfce4

License:        LGPL-2.0-or-later
URL:            https://gitlab.xfce.org/xfce/libxfce4util
Source0: https://archive.xfce.org/src/xfce/libxfce4util/4.21/libxfce4util-%{version}.tar.xz

# 4.21 builds with meson only (upstream dropped autotools).
BuildRequires:  gcc
BuildRequires:  meson
BuildRequires:  ninja-build
BuildRequires:  xfce4-dev-tools
BuildRequires:  glib2-devel
BuildRequires:  gobject-introspection-devel
BuildRequires:  vala
BuildRequires:  gettext

Requires:       glib2%{?_isa}

%description
Basic utility library for the Xfce desktop environment.
Required by all other Xfce components.

%package devel
Summary: Development files for %{name}
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
Headers, pkgconfig, GObject-Introspection typelib, and Vala bindings for
building against libxfce4util.

%prep
%autosetup -n libxfce4util-%{version}

%build
%meson -Dgtk-doc=false
%meson_build

%install
%meson_install
%find_lang libxfce4util

%post -p /sbin/ldconfig
%postun -p /sbin/ldconfig

%files -f libxfce4util.lang
%license COPYING
%doc NEWS README.md
%{_libdir}/libxfce4util.so.*
%{_sbindir}/xfce4-kiosk-query

%files devel
%{_includedir}/xfce4/libxfce4util/
%{_libdir}/libxfce4util.so
%{_libdir}/pkgconfig/libxfce4util*.pc
%{_libdir}/girepository-1.0/Libxfce4util-1.0.typelib
%{_datadir}/gir-1.0/Libxfce4util-1.0.gir
%{_datadir}/vala/vapi/libxfce4util-1.0.vapi
%{_datadir}/vala/vapi/libxfce4util-1.0.deps

%changelog
* Fri Nov 20 2026 James Reilly <jreilly1821@gmail.com> - 4.21.0-1
- Update to the 4.21.0 release tarball (XFCE 4.21 tag set shared with tuna-os/brussels)

* Sat Jun 27 2026 TunaOS Bot <bot@tunaos.org> - 4.20.1-17
- Initial XFCE Wayland package for TunaOS
