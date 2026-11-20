Name: garcon
Version: 4.21.0
Release: 1%{?dist}
Summary: Xfce menu handling library
License: LGPL-2.0-or-later
URL: https://gitlab.xfce.org/xfce/garcon
Source0: https://archive.xfce.org/src/xfce/garcon/4.21/garcon-%{version}.tar.xz
# 4.21 builds with meson only (upstream dropped autotools).
BuildRequires: gcc, meson, ninja-build, gettext
BuildRequires: xfce4-dev-tools
BuildRequires: gobject-introspection-devel
BuildRequires: glib2-devel, libxfce4util-devel
BuildRequires: gtk3-devel, libxfce4ui-devel
%description
Menu handling library for the Xfce desktop environment.

%package devel
Summary: Development files for %{name}
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
Headers and pkgconfig files for building against garcon.

%prep
%autosetup -n garcon-%{version}

%build
%meson -Dgtk-doc=false
%meson_build

%install
%meson_install
%find_lang %{name}

%files -f %{name}.lang
%license COPYING
%{_libdir}/libgarcon*.so.*
%{_sysconfdir}/xdg/menus/xfce-applications.menu
%{_datadir}/desktop-directories/*.directory
%{_datadir}/icons/hicolor/*/apps/org.xfce.garcon.png

%files devel
%{_includedir}/garcon-1/
%{_includedir}/garcon-gtk3-1/
%{_libdir}/libgarcon*.so
%{_libdir}/pkgconfig/*.pc
%{_libdir}/girepository-1.0/*.typelib
%{_datadir}/gir-1.0/*.gir

%changelog
* Fri Nov 20 2026 James Reilly <jreilly1821@gmail.com> - 4.21.0-1
- Update to the 4.21.0 release tarball (XFCE 4.21 tag set shared with tuna-os/brussels)

* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - %{version}-%{release}
- Packaged for the XFCE Wayland stack (tuna-os/tunaos-packages#65)

