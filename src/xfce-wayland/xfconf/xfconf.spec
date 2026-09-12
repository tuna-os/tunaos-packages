%global commit e916e66392f480691a31f0c8591fd02c7967327c
%global shortcommit %(c=%{commit}; echo ${c:0:7})
Name: xfconf
Version: 4.21.2
Release: 6%{?dist}
Summary: Xfce configuration daemon and library
License: GPL-2.0-or-later
URL: https://gitlab.xfce.org/xfce/xfconf
Source0: https://gitlab.xfce.org/xfce/xfconf/-/archive/%{commit}/xfconf-%{commit}.tar.gz
BuildRequires: gcc, meson, ninja-build
BuildRequires: glib2-devel, dbus-devel, libxfce4util-devel
BuildRequires: gobject-introspection-devel, vala, intltool, gettext
# xdt-gen-visibility (from xfce4-dev-tools) generates the symbol visibility
# header; meson.build calls find_program(..., required: true) for it, so
# meson setup fails before compiling anything without it. Required even
# though this commit has no autotools left: the script is independent of
# xdt-autogen. Fedora 44 ships xfce4-dev-tools-4.20.0; the EL10 chain builds
# it in the xfce-build-tools tier ahead of the core libs.
BuildRequires: xfce4-dev-tools
%description
Xfce configuration daemon and library. Required by all Xfce components.

%package devel
Summary: Development files for %{name}
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
Headers, pkgconfig, GObject-Introspection typelib, and Vala bindings for
building against xfconf.

%prep
%autosetup -n xfconf-%{commit}

# This commit has fully migrated to meson (no configure.ac/autogen.sh at
# all), so nothing here runs xdt-autogen; gtk-doc defaults to false in
# meson_options.txt already. xfce4-dev-tools is still a BuildRequires for
# xdt-gen-visibility alone (see above).
%build
%meson
%meson_build

%install
%meson_install
%find_lang xfconf

%files -f xfconf.lang
%license COPYING
%{_libdir}/libxfconf*.so.*
%{_libdir}/xfce4/xfconf/xfconfd
%{_bindir}/xfconf-query
%{_libdir}/gio/modules/libxfconfgsettingsbackend.so
%{_datadir}/bash-completion/completions/xfconf-query
%{_prefix}/lib/systemd/user/xfconfd.service
%{_datadir}/dbus-1/services/*.service

%files devel
%{_includedir}/xfce4/xfconf-0/xfconf/
%{_libdir}/libxfconf*.so
%{_libdir}/pkgconfig/*.pc
%{_libdir}/girepository-1.0/*.typelib
%{_datadir}/gir-1.0/*.gir
%{_datadir}/vala/vapi/*

%changelog
* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - %{version}-%{release}
- Packaged for the XFCE Wayland stack (tuna-os/tunaos-packages#65)

