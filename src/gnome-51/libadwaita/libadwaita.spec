%global apiver  1
# 4.23.1, not 4.21.1: libadwaita 1.10.beta.1's own meson.build declares
# gtk_min_version = '>= 4.23.1' (the exact tag this spec packages, verified
# against gitlab.gnome.org/GNOME/libadwaita, not the main branch, which can
# have drifted since this beta). The stale 4.21.1 floor was satisfiable by
# whatever gtk4 build happened to exist, including one still vendoring
# pango -- see gtk4.spec's pango_version comment for that failure mode.
# #580 fixed this by hand; Rawhide's own spec has since caught up to the
# same 4.23.1 floor, so this fork now inherits it directly instead of
# needing a standing correction -- comment kept for the institutional memory.
%global gtk_version 4.23.1
# Keep this aligned with the packaged release's meson.build. A lower floor can
# let dependency resolution select an older GLib than libadwaita 1.10.0 accepts.
%global glib_version 2.89.3

Name:           libadwaita
Version:        1.10.0
Release:        %autorelease
Summary:        Building blocks for modern GNOME applications

# part of src/adw-spring-animation.c is MIT
License:        LGPL-2.1-or-later AND MIT
URL:            https://gitlab.gnome.org/GNOME/libadwaita
Source0:        https://download.gnome.org/sources/%{name}/%{gnome_major_minor_version}/%{name}-%{gnome_tarball_version}.tar.xz

# The sassc tarball-build fix carried for 1.10.beta.1 is included in 1.10.0:
# src/stylesheet/meson.build checks for the gtk.css shipped in release tarballs.
# Keep sassc out of BuildRequires; it is only needed for git checkouts.

%gnome_check_version

BuildRequires:  desktop-file-utils
BuildRequires:  gcc
BuildRequires:  gettext
BuildRequires:  gi-docgen
BuildRequires:  libappstream-glib
BuildRequires:  meson >= 0.63.0
BuildRequires:  vala
BuildRequires:  pkgconfig(appstream)
BuildRequires:  pkgconfig(fribidi)
BuildRequires:  pkgconfig(glib-2.0) >= %{glib_version}
BuildRequires:  pkgconfig(gobject-introspection-1.0)
BuildRequires:  pkgconfig(gtk4) >= %{gtk_version}

Requires:       gtk4%{?_isa} >= %{gtk_version}

%description
Building blocks for modern GNOME applications.


%package        devel
Summary:        Development files for %{name}

Requires:       %{name}%{?_isa} = %{version}-%{release}
Requires:       vala
Recommends:     %{name}-doc = %{version}-%{release}
Suggests:       %{name}-demo = %{version}-%{release}

%description    devel
Development files for %{name}.


%package        doc
Summary:        Documentation files for %{name}
BuildArch:      noarch

Recommends:     %{name}-devel = %{version}-%{release}
# Because web fonts from upstream are not bundled in the gi-docgen package,
# packages containing documentation generated with gi-docgen should depend on
# this metapackage to ensure the proper system fonts are present.
Recommends:     gi-docgen-fonts

%description    doc
Documentation files for %{name}.


%package        demo
Summary:        Demo files for %{name}
BuildArch:      noarch

Requires:       %{name} = %{version}-%{release}
Suggests:       %{name}-devel = %{version}-%{release}

%description    demo
Demo files for %{name}.


%prep
%autosetup -p1 -n %{name}-%{gnome_tarball_version}


%build
# Explicit meson setup/ninja, not the meson RPM macros (meson, meson_build,
# meson_install) that Rawhide's current spec uses: glib2.spec in this tree
# (see its changelog) had to revert away from those macros because "fg: no
# job control" fails the build under COPR-style builders that run rpmbuild
# under --console=pipe non-interactive bash. Keeping the explicit form here
# avoids reintroducing that failure mode.
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
    -Ddocumentation=true
ninja -C _build -j%{_smp_build_ncpus}


%install
DESTDIR=%{buildroot} ninja -C _build install
%find_lang %{name}


%check
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/*.xml
desktop-file-validate %{buildroot}%{_datadir}/applications/*.desktop


%files -f %{name}.lang
%license COPYING
%doc README.md AUTHORS NEWS
%{_bindir}/adwaita-%{apiver}-demo
%{_libdir}/%{name}-%{apiver}.so.0
%{_libdir}/girepository-1.0/*.typelib

%files devel
%dir %{_datadir}/gir-1.0
%{_datadir}/gir-1.0/*-%{apiver}.gir
%{_datadir}/vala/vapi/%{name}-%{apiver}.*
%{_includedir}/%{name}-%{apiver}/
%{_libdir}/%{name}-%{apiver}.so
%{_libdir}/pkgconfig/*-%{apiver}.pc

%files doc
%{_docdir}/%{name}-%{apiver}/

%files demo
%{_datadir}/applications/*.desktop
%{_datadir}/icons/hicolor/*/apps/*.svg
%{_metainfodir}/*.metainfo.xml


%changelog
%autochangelog
