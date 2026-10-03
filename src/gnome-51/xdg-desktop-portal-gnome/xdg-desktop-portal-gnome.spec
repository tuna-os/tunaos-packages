# Kept as our own %%global instead of Rawhide's newer %%gnome_check_version /
# %%{gnome_major_version} / %%{gnome_tarball_version} macro trio: zero other
# specs in src/gnome-51/*/*.spec use those macros (including every sibling
# already re-forked from Rawhide this session -- gnome-desktop3,
# gnome-control-center, gnome-online-accounts), so they are not assumed
# present in the hummingbird-ci buildroot.
%global tarball_version %%(echo %{version} | tr '~' '.')

# src/meson.build: dependency('xdg-desktop-portal', version: '>= 1.21.1').
# Rawhide's own live spec already carries this same 1.21.1 floor (fetched
# 2026-08-28), so #580's fix (e918d72) is no longer a local-only delta -- it
# has since landed upstream too. Kept explicit here regardless, since dnf5
# builddep only enforces what this file states, not what meson.build states.
%global xdg_desktop_portal_version 1.21.1

Name:           xdg-desktop-portal-gnome
Version:        51.0
Release:        %autorelease
Summary:        Backend implementation for xdg-desktop-portal using GNOME

License:        LGPL-2.1-or-later
URL:            https://gitlab.gnome.org/GNOME/%{name}
Source0:        https://download.gnome.org/sources/%{name}/51/%{name}-%{tarball_version}.tar.xz

BuildRequires:  desktop-file-utils
BuildRequires:  gcc
BuildRequires:  gettext
BuildRequires:  meson
BuildRequires:  pkgconfig(fontconfig)
BuildRequires:  pkgconfig(gnome-bg-4)
BuildRequires:  pkgconfig(gnome-desktop-4)
BuildRequires:  pkgconfig(gsettings-desktop-schemas)
BuildRequires:  pkgconfig(gtk4)
BuildRequires:  pkgconfig(libadwaita-1)
BuildRequires:  pkgconfig(xdg-desktop-portal) >= %{xdg_desktop_portal_version}
BuildRequires:  systemd-rpm-macros
Requires:       dbus
Requires:       dbus-common
Requires:       xdg-desktop-portal >= %{xdg_desktop_portal_version}
Supplements:    gnome-shell

# https://github.com/containers/composefs/pull/229#issuecomment-1838735764
%if 0%{?rhel} >= 10
ExcludeArch:    %{ix86}
%endif

%description
A backend implementation for xdg-desktop-portal that is using various pieces of
GNOME infrastructure, such as the org.gnome.Shell.Screenshot or
org.gnome.SessionManager D-Bus interfaces.


%prep
%autosetup -p1 -n %{name}-%{tarball_version}


%build
# Rawhide's current spec has since collapsed the hand-rolled `meson setup`
# (every path flag spelled out, pre-%%meson-macro Fedora packaging style) plus
# `DESTDIR=%%{buildroot} ninja -C _build install` down to the %%meson /
# %%meson_build / %%meson_install macro trio. Adopted here too: the same form
# already proven in this repo's hummingbird-ci mock config by the
# xdg-desktop-portal fork (src/gnome-51/xdg-desktop-portal/xdg-desktop-portal.spec)
# and by gnome-desktop3's re-fork.
%meson -Dsystemduserunitdir=%{_userunitdir}
%meson_build

%install
%meson_install
desktop-file-validate %{buildroot}/%{_datadir}/applications/%{name}.desktop
%find_lang %{name}


%post
%systemd_user_post %{name}.service

%preun
%systemd_user_preun %{name}.service


%files -f %{name}.lang
%license COPYING
%doc NEWS README.md
%{_libexecdir}/%{name}
%{_datadir}/applications/%{name}.desktop
%{_datadir}/dbus-1/services/org.freedesktop.impl.portal.desktop.gnome.service
%{_datadir}/glib-2.0/schemas/xdg-desktop-portal-gnome.gschema.xml
%{_datadir}/xdg-desktop-portal/portals/gnome.portal
%{_userunitdir}/%{name}.service


%changelog
%autochangelog
