%global glib2_version 2.86.0
%global gobject_introspection_version 1.72.0
# Rawhide's gjs-1.89.2 currently floors mozjs140 at 140.13.0, but
# src/deps/mozjs140/mozjs140.spec in this repo only builds 140.6.0 as of
# 2026-08-28. Pinning to Rawhide's floor here would make gjs's own
# BuildRequires/Requires unsatisfiable against our own mozjs140 once the full
# hummingbird chain builds gjs against the local repo (a plain `--package gjs`
# verify build won't catch this: it resolves mozjs140 from the real Fedora
# Rawhide repo in the buildroot instead of our local one, which quietly hides
# the gap). Bump this to 140.13.0 in lockstep with mozjs140, not before.
%global mozjs140_version 140.6.0

# xwfb-run + mutter GUI tests (Rawhide's %check) don't have a working
# compositor in our mock/COPR builders -- same reason gtk4/nautilus/mutter's
# forks in this repo skip GUI-heavy %check content. Default tests off; the
# tests subpackage and its BuildRequires stay available for anyone who wants
# to opt in locally.
%bcond_with tests

Name:           gjs
Version:        1.90.0
Release:        %autorelease
Summary:        Javascript Bindings for GNOME

# The following files contain code from Mozilla which
# is triple licensed under MPL-1.1/GPL-2.0-or-later/LGPL-2.1-or-later:
# The console module (modules/console.c)
# Stack printer (gjs/stack.c)
# modules/esm/_encoding/util.js and few other things are MIT
# modules/script/tweener/equations.js is BSD-3-Clause
License:        MIT AND BSD-3-Clause AND (MIT OR LGPL-2.0-or-later) AND (MPL-1.1 OR GPL-2.0-or-later OR LGPL-2.1-or-later)
URL:            https://wiki.gnome.org/Projects/Gjs
Source0:        https://download.gnome.org/sources/%{name}/%{gnome_major_minor_version}/%{name}-%{version}.tar.xz

BuildRequires:  gcc-c++
BuildRequires:  meson
BuildRequires:  gettext
BuildRequires:  readline-devel
BuildRequires:  pkgconfig(cairo-gobject)
BuildRequires:  pkgconfig(gio-2.0) >= %{glib2_version}
BuildRequires:  pkgconfig(gobject-introspection-1.0) >= %{gobject_introspection_version}
BuildRequires:  pkgconfig(gtk4)
BuildRequires:  pkgconfig(mozjs-140) >= %{mozjs140_version}
BuildRequires:  pkgconfig(sysprof-capture-4)
# gjs's meson.build probes for dbus-run-session at configure time regardless
# of -Dinstalled_tests, so this has to stay outside the %%{with tests} guard
# below or configure fails even with tests disabled.
BuildRequires:  /usr/bin/dbus-run-session

%if %{with tests}
BuildRequires:  gtk3
BuildRequires:  dbus-x11
BuildRequires:  mesa-dri-drivers
BuildRequires:  mutter
BuildRequires:  xwayland-run
%endif

Requires: glib2%{?_isa} >= %{glib2_version}
Requires: gobject-introspection%{?_isa} >= %{gobject_introspection_version}
Requires: mozjs140%{?_isa} >= %{mozjs140_version}

%description
Gjs allows using GNOME libraries from Javascript. It's based on the
Spidermonkey Javascript engine from Mozilla and the GObject introspection
framework.

%package devel
Summary: Development package for %{name}
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
Files for development with %{name}.

%package tests
Summary: Tests for the gjs package
# installed-tests/js/modules/encodings.json is BSD-3-Clause
License: MIT AND (MIT OR LGPL-2.0-or-later) AND BSD-3-Clause
Requires: %{name}%{?_isa} = %{version}-%{release}

%if %{with tests}
%description tests
The gjs-tests package contains tests that can be used to verify
the functionality of the installed gjs package.
%else
%description tests
Tests are disabled.
%endif

%prep
%autosetup -p1

%build
# Explicit meson setup/ninja/DESTDIR install instead of %%meson/%%meson_build/
# %%meson_install: repo-wide convention (see gtk4, libadwaita) because the
# macro-based flow hit "fg: no job control" on our COPR/mock-runner builders.
meson setup _build \
    --buildtype=plain \
    --prefix=%{_prefix} \
    --libdir=%{_libdir} \
    --libexecdir=%{_libexecdir} \
    --bindir=%{_bindir} \
    --sbindir=%{_sbindir} \
    --includedir=%{_includedir} \
    --datadir=%{_datadir} \
    --mandir=%{_mandir} \
    --infodir=%{_infodir} \
    --localedir=%{_datadir}/locale \
    --sysconfdir=%{_sysconfdir} \
    --localstatedir=%{_localstatedir} \
    --sharedstatedir=%{_sharedstatedir} \
    --wrap-mode=nodownload \
    -Dinstalled_tests=false
ninja -C _build -j%{_smp_build_ncpus}

%install
DESTDIR=%{buildroot} ninja -C _build install

%files
# gjs's meson.build installs the installed-tests support libraries
# (libgimarshallingtests.so, libregress.so, libutility.so, libwarnlib.so and
# their typelibs) unconditionally -- -Dinstalled_tests=false above does NOT
# suppress them. With tests off by default (%%bcond_with tests, see the top of
# this spec) nothing claims them, so rpmbuild's unpackaged-files check fails
# without this exclude. Confirmed against a real build in this repo
# (2026-08-28): dropping this line reproduces
# "error: Installed (but unpackaged) file(s) found" for exactly these paths.
%exclude %{_libexecdir}/installed-tests/
%exclude %{_datadir}/installed-tests/
%license COPYING
%doc NEWS README.md
%{_bindir}/gjs
%{_bindir}/gjs-console
%{_libdir}/gjs/
%{_libdir}/libgjs.so.0*

%files devel
%doc examples/*
%{_includedir}/gjs-1.0
%{_libdir}/pkgconfig/gjs-1.0.pc
%{_libdir}/libgjs.so
%dir %{_datadir}/gjs-1.0
%{_datadir}/gjs-1.0/lsan/
%{_datadir}/gjs-1.0/valgrind/

%files tests
%if %{with tests}
%{_libexecdir}/installed-tests/
%{_datadir}/glib-2.0/schemas/org.gnome.GjsTest.gschema.xml
%{_datadir}/installed-tests/
%endif

%changelog
%autochangelog
