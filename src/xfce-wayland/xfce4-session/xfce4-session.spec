Name:           xfce4-session
Version:        4.21.2
Release:        1%{?dist}
Summary:        Session manager for the Xfce desktop environment (Wayland)

License:        GPL-2.0-or-later
URL:            https://gitlab.xfce.org/xfce/xfce4-session
Source0: https://archive.xfce.org/src/xfce/xfce4-session/4.21/xfce4-session-%{version}.tar.xz

BuildRequires: gcc
BuildRequires:  gtk3-devel
BuildRequires:  libxfce4ui-devel
BuildRequires:  libxfce4util-devel
BuildRequires:  libxfce4windowing-devel
BuildRequires:  xfconf-devel
BuildRequires:  polkit-devel
BuildRequires:  systemd-devel
BuildRequires:  dbus-devel
BuildRequires:  intltool
BuildRequires:  gettext
BuildRequires:  meson
BuildRequires:  ninja-build
BuildRequires:  gtk-layer-shell-devel
# 4.21.2 builds libxfce4session-client (GIR + Vala bindings) and its
# xdg-session-management Wayland client code.
BuildRequires:  gobject-introspection-devel
BuildRequires:  vala
BuildRequires:  wayland-devel
# staging/xdg-session-management first shipped in wayland-protocols 1.48.
BuildRequires:  wayland-protocols-devel >= 1.48

Requires:       polkit
Requires:       dbus

%description
Xfce4-session is the session manager for the Xfce desktop environment.
It is responsible for starting the Xfce desktop, saving and restoring
sessions, and managing logout/shutdown/reboot actions. This build supports
Wayland sessions via the wayland-sessions entry point.

%package devel
Summary: Development files for libxfce4session-client
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
Headers, pkgconfig, GObject-Introspection data and Vala bindings for
libxfce4session-client, the session-management client library that
xfce4-panel, xfdesktop and thunar link against from 4.21.

%prep
%autosetup -n xfce4-session-%{version}

%build
# Wayland-only build (matches this stack's convention elsewhere).
%meson -Dx11=disabled
%meson_build

%install
%meson_install
%find_lang %{name}

%files -f %{name}.lang
%license COPYING
%{_bindir}/xfce4-session*
%{_bindir}/startxfce4
%{_bindir}/xflock4
%{_datadir}/wayland-sessions/*.desktop
%{_datadir}/applications/*.desktop
%{_datadir}/xfce4/labwc/
%{_datadir}/icons/hicolor/*/apps/org.xfce.session.*
%{_datadir}/icons/hicolor/*/actions/xfsm-*.png
%{_datadir}/man/man1/xfce4-session*.1*
%{_datadir}/polkit-1/actions/org.xfce.session.policy
%{_datadir}/xdg-desktop-portal/xfce-portals.conf
%{_sysconfdir}/xdg/xfce4/xinitrc
%{_sysconfdir}/xdg/xfce4/Xft.xrdb
%{_sysconfdir}/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml
# xfsm-shutdown-helper installs under $libdir (helper_path_prefix defaults
# to $prefix/$libdir), not $libexecdir.
%{_libdir}/xfce4/session/
%{_libdir}/libxfce4session-client-1.so.*
%{_libdir}/girepository-1.0/Libxfce4sessionclient-*.typelib

%files devel
%{_includedir}/xfce4/libxfce4session-client-1/
%{_libdir}/libxfce4session-client-1.so
%{_libdir}/pkgconfig/libxfce4session-client-1.pc
%{_datadir}/gir-1.0/Libxfce4sessionclient-*.gir
%{_datadir}/vala/vapi/libxfce4session-client-1.*

%changelog
* Fri Nov 20 2026 James Reilly <jreilly1821@gmail.com> - 4.21.2-1
- Update to the 4.21.2 release tarball (XFCE 4.21 tag set shared with tuna-os/brussels)

* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - 4.21.0-1
- Initial XFCE Wayland package for TunaOS EL10
