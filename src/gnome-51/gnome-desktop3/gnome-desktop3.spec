# Forked from Fedora Rawhide's gnome-desktop3.spec (fetched 2026-08-28) to
# stop this file from silently drifting the way it had: the gtk4 floor below
# had rotted to 4.4.0 against upstream's actual >= 4.12.0 (verified against
# gnome-desktop's own meson.build at the 51.alpha tag we build), the same
# class of bug #580 fixed in gtk4.spec/libadwaita.spec. Rawhide's build and
# install steps now use the standard %%meson/%%meson_build/%%meson_install
# macros (already proven working in this repo's hummingbird-ci mock config
# by the xdg-desktop-portal fork) instead of a hand-rolled meson invocation,
# so we adopt that structure too.
#
# Hummingbird-specific deltas kept on top of Rawhide's structure, each
# commented at its point of use below:
#   - No use of the %%gnome_check_version / %%{gnome_major_version} /
#     %%{gnome_tarball_version} macros.
#     Nothing else in this repo's gnome-51 tree uses those macros (checked:
#     zero hits across src/gnome-51/*/*.spec, including the other already-
#     rebased forks), so they are not assumed present in the hummingbird-ci
#     buildroot. We keep our own %%{tarball_version} global instead.
#   - -Dlegacy_library=false (drops the gtk3-era libgnome-desktop-3.so and
#     its gtk3 runtime dependency -- per af46f91, hummingbird's desktop
#     stack is GTK4-only and does not want this library, regardless of
#     whether gtk3 itself is otherwise present in the tree for other
#     consumers).
#   - %files trimmed to match what legacy_library=false actually builds
#     (this is the #580 fix -- see the comment above %files for the story).

%global gdk_pixbuf2_version               2.36.5
%global gtk4_version                      4.12.0
%global glib2_version                     2.53.0
%global gsettings_desktop_schemas_version 3.27.0
%global po_package                        gnome-desktop-3.0

%global tarball_version %%(echo %{version} | tr '~' '.')

Name:    gnome-desktop3
Version: 51.0
Release: %autorelease
Summary: Library with common API for various GNOME modules

License: GPL-2.0-or-later AND LGPL-2.0-or-later AND GFDL-1.1-or-later
URL:     https://gitlab.gnome.org/GNOME/gnome-desktop
Source:  https://download.gnome.org/sources/gnome-desktop/51/gnome-desktop-%{tarball_version}.tar.xz

BuildRequires: gcc
BuildRequires: gettext
BuildRequires: gtk-doc
BuildRequires: itstool
BuildRequires: meson
BuildRequires: pkgconfig(gdk-pixbuf-2.0) >= %{gdk_pixbuf2_version}
BuildRequires: pkgconfig(gio-2.0) >= %{glib2_version}
BuildRequires: pkgconfig(glib-2.0) >= %{glib2_version}
BuildRequires: pkgconfig(gobject-introspection-1.0)
BuildRequires: pkgconfig(gsettings-desktop-schemas) >= %{gsettings_desktop_schemas_version}
# No pkgconfig(gtk+-3.0) BuildRequires: -Dlegacy_library=false below makes
# gtk3_dep non-required in gnome-desktop's own meson.build, and the gtk3-only
# library it would build is exactly what hummingbird does not want (see the
# top-of-file note).
BuildRequires: pkgconfig(gtk4) >= %{gtk4_version}
BuildRequires: pkgconfig(iso-codes)
BuildRequires: pkgconfig(libseccomp)
BuildRequires: pkgconfig(libudev)
BuildRequires: pkgconfig(xkeyboard-config)
BuildRequires: python3
BuildRequires: python3dist(langtable)

%if !0%{?flatpak}
Requires: bubblewrap
%endif
Requires: gdk-pixbuf2%{?_isa} >= %{gdk_pixbuf2_version}
Requires: glib2%{?_isa} >= %{glib2_version}
# needed for GnomeWallClock
Requires: gsettings-desktop-schemas >= %{gsettings_desktop_schemas_version}

# GnomeBGSlideShow API change breaks older gnome-shell versions
Conflicts: gnome-shell < 3.33.4

%description
gnome-desktop3 contains the libgnome-desktop library as well as a data
file that exports the "GNOME" version to the Settings Details panel.

The libgnome-desktop library provides API shared by several applications
on the desktop, but that cannot live in the platform for various
reasons. There is no API or ABI guarantee, although we are doing our
best to provide stability. Documentation for the API is available with
gtk-doc.

%package devel
Summary: Libraries and headers for %{name}
License: LGPL-2.0-or-later
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
The %{name}-devel package contains libraries and header files for
developing applications that use %{name}.

%package -n gnome-desktop4
Summary: Library with common API for various GNOME modules
License: GPL-2.0-or-later AND LGPL-2.0-or-later
# Depend on base package for translations, help, and version.
Requires: %{name}%{?_isa} = %{version}-%{release}

%description -n gnome-desktop4
gnome-desktop4 contains the libgnome-desktop library.

The libgnome-desktop library provides API shared by several applications
on the desktop, but that cannot live in the platform for various
reasons. There is no API or ABI guarantee, although we are doing our
best to provide stability.

%package -n gnome-desktop4-devel
Summary: Libraries and headers for gnome-desktop4
License: LGPL-2.0-or-later
Requires: gnome-desktop4%{?_isa} = %{version}-%{release}

%description -n gnome-desktop4-devel
The gnome-desktop4-devel package contains libraries and header files for
developing applications that use gnome-desktop4.

%package  tests
Summary:  Tests for the %{name} package
Requires: %{name}%{?_isa} = %{version}-%{release}

%description tests
The %{name}-tests package contains tests that can be used to verify
the functionality of the installed %{name} package.

%prep
%autosetup -p1 -n gnome-desktop-%{tarball_version}

%build
%meson -Dgtk_doc=true -Dinstalled_tests=true -Dlegacy_library=false
%meson_build

%install
%meson_install

%find_lang %{po_package} --all-name --with-gnome

%files -f %{po_package}.lang
%doc AUTHORS NEWS README.md
%license COPYING COPYING.LIB
# gnome-desktop-debug is installed only when legacy_library=true (the gtk3
# build). %build passes -Dlegacy_library=false unconditionally -- for every
# target, not just EL10 -- so this directory never exists and a %{?rhel}
# guard here was checking the wrong condition: it included the path on
# every NON-rhel target (this spec's own hummingbird-ci mock config among
# them), where legacy_library is equally false. Confirmed by an actual
# build: `error: Directory not found: .../usr/libexec/gnome-desktop-debug`.
# For the same reason, this package also carries none of the legacy 3.x
# runtime files (libgnome-desktop-3.so.*, its typelib) that Rawhide's
# upstream spec ships -- they are never built here either.

%files devel
%dir %{_datadir}/gtk-doc/
%dir %{_datadir}/gtk-doc/html/
%doc %{_datadir}/gtk-doc/html/gnome-desktop3/

%files -n gnome-desktop4
%doc AUTHORS NEWS README.md
%license COPYING COPYING.LIB
# LGPL
%{_libdir}/libgnome-bg-4.so.2{,.*}
%{_libdir}/libgnome-desktop-4.so.2{,.*}
# GNOME 51 drops libgnome-rr-4 (display configuration moved out) and adds the
# QR libraries, which carry their own soversion 0 rather than 2 -- verified
# against the 51.alpha tarball's meson.options/meson.build and against what
# the build actually produced (libgnome-qr-4.so.0.0.1,
# libgnome-qr-gtk-4.so.0.0.1). The -devel globs already cover their .so,
# .pc and .gir; only the runtime sonames are spelled out here.
%{_libdir}/libgnome-qr-4.so.0{,.*}
%{_libdir}/libgnome-qr-gtk-4.so.0{,.*}
%{_libdir}/girepository-1.0/Gnome*-4.0.typelib

%files -n gnome-desktop4-devel
%{_libdir}/libgnome-*-4.so
%{_libdir}/pkgconfig/gnome-*-4.pc
%{_includedir}/gnome-desktop-4.0
%{_datadir}/gir-1.0/Gnome*-4.0.gir

%files tests
%{_libexecdir}/installed-tests/gnome-desktop
%{_datadir}/installed-tests

%changelog
%autochangelog
