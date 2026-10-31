Name: libxfce4windowing
Version: 4.20.6
Release: 1%{?dist}
Summary: X11/Wayland windowing abstraction for Xfce
License: LGPL-2.0-or-later
URL: https://gitlab.xfce.org/xfce/libxfce4windowing
Source0: https://archive.xfce.org/src/xfce/libxfce4windowing/4.20/libxfce4windowing-%{version}.tar.bz2
BuildRequires: gcc, gtk3-devel, libxfce4util-devel, wayland-devel
BuildRequires: wayland-protocols-devel, libdisplay-info-devel, meson
%if ! 0%{?eln}
BuildRequires: libX11-devel, libXrandr-devel, libwnck3-devel
%endif
BuildRequires: gobject-introspection-devel, vala
%description
Windowing abstraction library for Xfce that handles both X11 and Wayland.
Required by xfce4-panel, xfdesktop, and other Wayland-aware components.

%package devel
Summary: Development files for %{name}
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
Headers and pkgconfig files for building against libxfce4windowing.

%prep
%autosetup -n libxfce4windowing-%{version}
%build
%if 0%{?eln}
# Fedora ELN ships no libwnck3, so build the Wayland backend only there.
# Every consumer in this stack already builds with -Dx11=disabled.
%meson -Dx11=disabled
%else
%meson
%endif
%meson_build
%install
%meson_install
%find_lang %{name}

%files -f %{name}.lang
%license COPYING
%{_libdir}/libxfce4windowing*.so.*
%{_libdir}/girepository-1.0/*.typelib

%files devel
%{_includedir}/xfce4/libxfce4windowing-0/
%{_libdir}/libxfce4windowing*.so
%{_libdir}/pkgconfig/*.pc
%{_datadir}/gir-1.0/*.gir
%{_datadir}/vala/vapi/*

%changelog
* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - %{version}-%{release}
- Packaged for the XFCE Wayland stack (tuna-os/tunaos-packages#65)

