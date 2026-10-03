%if 0%{?fedora}
%global with_broadway 1
%endif

%global glib2_version 2.89.3
# Load-bearing, not cosmetic: GTK 4.24.0's own meson.build declares
# pango_major_req=1, pango_minor_req=58. A floor below 1.58 is satisfiable
# by an older pango, which used to make gtk4 silently vendor its own copy
# of pango as a meson subproject instead of failing the BuildRequires
# (#567/#580). Guarded by tests/test_gtk4_and_libadwaita_floors_match_upstream.py
# -- don't lower this without re-checking that test.
%global pango_version 1.58.0
%global cairo_version 1.18.2
%global gdk_pixbuf_version 2.30.0
%global gstreamer_version 1.24.0
%global harfbuzz_version 8.4.0
%global wayland_protocols_version 1.48
%global wayland_version 1.24.0
%global epoxy_version 1.4

%global bin_version 4.0.0

# Filter provides for private modules
%global __provides_exclude_from ^%{_libdir}/gtk-4.0

# FTBFS on i686 with GCC 14 -Werror=int-conversion
# https://gitlab.gnome.org/GNOME/gtk/-/issues/6033
%if 0%{?fedora} >= 40 || 0%{?rhel} >= 10
%ifarch %{ix86}
%global build_type_safety_c 1
%endif
%endif

Name:           gtk4
Version:        4.24.0
Release:        1%{?dist}
Summary:        GTK graphical user interface library

# Rawhide's current spec sources the download.gnome.org path segment from a
# %{gnome_major_minor_version} macro that Fedora's GNOME SIG tooling injects
# into the Rawhide buildroot; it is not defined in our el10/hummingbird mock
# config. Compute it locally instead (same pattern already used by
# src/gnome-51/gobject-introspection.spec) so Source0 stays correct on
# version bumps without depending on an undefined macro. Must be defined
# after Version: is set, since %%() shell expansion runs eagerly.
%global gnome_major_minor_version %(echo %{version} | cut -d. -f1-2)

# Most files are either LGPL-2.0-or-later or LGPL-2.1-or-later.
# gtk/roaring/ and gtk/timsort/ are Apache-2.0
# .editorconfig is CC0-1.0
# po/kg.po is LGPL-2.0-or-later
# po/po2tbl.sed.in is GPL-2.0-or-later
# tests/testscrolledge.c is GPL-3.0-or-later
# gdk/macos/gdkmacoskeymap.c and testsuite/gsk/shader.c are (LGPL-2.1-or-later AND MIT)
# .gitlab-ci/pages/fonts.css is LGPL-2.1-or-later AND OFL-1.0 but omitted here because it's not part of the binary RPM
# gdk/x11/gdkxftdefaults.c LGPL-2.1-or-later AND HPND-sell-variant
# gdk/x11/gdkasync.c is LGPL-2.1-or-later AND MIT-open-group
# gdk/win32/winpointer.h is LGPL-2.0-or-later AND ZPL-2.1
# The following files are HPND-sell-variant:
#  gdk/x11/xsettings-client.c
#  gdk/x11/xsettings-client.h
#  gtk/gtk-text-input.xml
#  gtk/text-input-unstable-v3.xml
# The following files are MIT:
#  demos/gtk-demo/css_multiplebgs.css
#  demos/gtk-demo/gtkgears.c
#  gdk/win32/gdkkeys-win32-impl-wow64.c
#  gdk/win32/gdkkeys-win32-impl.c
#  gdk/win32/gdkkeys-win32.h
#  gtk/inspector/css-editor.c
#  gtk/inspector/css-editor.h
#  gtk/inspector/css-node-tree.h
#  gtk/inspector/init.c
#  gtk/inspector/inspect-button.c
#  gtk/inspector/logs.c
#  gtk/inspector/logs.h
#  gtk/inspector/object-tree.c
#  gtk/inspector/object-tree.h
#  gtk/inspector/prop-list.c
#  gtk/inspector/prop-list.h
#  gtk/inspector/window.c
#  gtk/inspector/window.h
#  tests/gtkgears.c
# testsuite/gsk/fonts/Cantarell-VF.otf is OFL-1.1
#
# The license was last checked for GTK 4.19.3.
License:        LGPL-2.0-or-later AND LGPL-2.1-or-later AND Apache-2.0 AND CC0-1.0 AND MIT AND MIT-open-group AND HPND-sell-variant AND GPL-2.0-or-later AND GPL-3.0-or-later AND OFL-1.1
URL:            https://www.gtk.org
Source0:        https://download.gnome.org/sources/gtk/%{gnome_major_minor_version}/gtk-%{version}.tar.xz

BuildRequires:  cups-devel
BuildRequires:  desktop-file-utils
BuildRequires:  docbook-style-xsl
BuildRequires:  gcc
BuildRequires:  gcc-c++
BuildRequires:  gettext
BuildRequires:  gi-docgen
BuildRequires:  glslc
BuildRequires:  meson
BuildRequires:  python3-gobject
BuildRequires:  pkgconfig(avahi-gobject)
BuildRequires:  pkgconfig(cairo) >= %{cairo_version}
BuildRequires:  pkgconfig(cairo-gobject) >= %{cairo_version}
BuildRequires:  pkgconfig(colord)
BuildRequires:  pkgconfig(egl)
BuildRequires:  pkgconfig(epoxy)
BuildRequires:  pkgconfig(libdrm)
BuildRequires:  pkgconfig(gdk-pixbuf-2.0) >= %{gdk_pixbuf_version}
BuildRequires:  pkgconfig(glib-2.0) >= %{glib2_version}
BuildRequires:  pkgconfig(gobject-introspection-1.0)
BuildRequires:  pkgconfig(graphene-gobject-1.0)
# gstreamer1-plugins-bad-free-devel pulls in libgtk-3 on EL10 (gtk3 removed); skip on RHEL
%if !0%{?rhel}
BuildRequires:  pkgconfig(gstreamer-player-1.0) >= %{gstreamer_version}
%endif
BuildRequires:  pkgconfig(harfbuzz) >= %{harfbuzz_version}
BuildRequires:  pkgconfig(json-glib-1.0)
BuildRequires:  pkgconfig(libjpeg)
BuildRequires:  pkgconfig(libpng)
BuildRequires:  pkgconfig(librsvg-2.0)
BuildRequires:  pkgconfig(libtiff-4)
BuildRequires:  pkgconfig(pango) >= %{pango_version}
BuildRequires:  pkgconfig(sysprof-capture-4)
# tracker3/tinysparql not available on EL10; disable file search integration
%if !0%{?rhel}
BuildRequires:  pkgconfig(tracker-sparql-3.0)
%endif
BuildRequires:  pkgconfig(vulkan)
BuildRequires:  pkgconfig(wayland-client) >= %{wayland_version}
BuildRequires:  pkgconfig(wayland-cursor) >= %{wayland_version}
BuildRequires:  pkgconfig(wayland-egl) >= %{wayland_version}
BuildRequires:  pkgconfig(wayland-protocols) >= %{wayland_protocols_version}
BuildRequires:  pkgconfig(xcomposite)
BuildRequires:  pkgconfig(xcursor)
BuildRequires:  pkgconfig(xdamage)
BuildRequires:  pkgconfig(xfixes)
BuildRequires:  pkgconfig(xi)
BuildRequires:  pkgconfig(xinerama)
BuildRequires:  pkgconfig(xkbcommon)
BuildRequires:  pkgconfig(xrandr)
BuildRequires:  pkgconfig(xrender)
BuildRequires:  /usr/bin/appstream-util
BuildRequires:  /usr/bin/rst2man

# standard icons
Requires: adwaita-icon-theme
# required for icon theme apis to work
Requires: hicolor-icon-theme
# split out in a subpackage
Requires: gtk-update-icon-cache
# for mime directory ownership
Requires: shared-mime-info

Requires: cairo%{?_isa} >= %{cairo_version}
Requires: cairo-gobject%{?_isa} >= %{cairo_version}
Requires: glib2%{?_isa} >= %{glib2_version}
Requires: harfbuzz%{?_isa} >= %{harfbuzz_version}
Requires: libepoxy%{?_isa} >= %{epoxy_version}
# gstreamer1-plugins-bad-free-libs depends on gtk3 on EL10; skip on RHEL
%if !0%{?rhel}
Requires: gstreamer1-plugins-bad-free-libs%{?_isa} >= %{gstreamer_version}
%endif
Requires: libwayland-client%{?_isa} >= %{wayland_version}
Requires: libwayland-cursor%{?_isa} >= %{wayland_version}
Requires: pango%{?_isa} >= %{pango_version}

# make sure we have a reasonable gsettings backend
Recommends: dconf%{?_isa}

%description
GTK is a multi-platform toolkit for creating graphical user
interfaces. Offering a complete set of widgets, GTK is suitable for
projects ranging from small one-off tools to complete application
suites.

This package contains version 4 of GTK.

%package devel
Summary: Development files for GTK
Requires: gtk4%{?_isa} = %{version}-%{release}

%description devel
This package contains the libraries and header files that are needed
for writing applications with version 4 of the GTK widget toolkit.

%package devel-docs
Summary: Developer documentation for GTK
BuildArch: noarch
Requires: gtk4 = %{version}-%{release}
# Because web fonts from upstream are not bundled in the gi-docgen package,
# packages containing documentation generated with gi-docgen should depend on
# this metapackage to ensure the proper system fonts are present.
Recommends: gi-docgen-fonts

%description devel-docs
This package contains developer documentation for version 4 of the GTK
widget toolkit.

%package devel-tools
Summary: Developer tools for GTK
Requires: gtk4%{?_isa} = %{version}-%{release}

%description devel-tools
This package contains helpful applications for developers using GTK.

%prep
%autosetup -p1 -n gtk-%{version}

%build
export CFLAGS='-std=c11 -fno-strict-aliasing -DG_DISABLE_CAST_CHECKS -DG_DISABLE_ASSERT %optflags'
# --wrap-mode=nodownload and --auto-features=enabled are pinned explicitly
# here (Rawhide's own %%meson invocation leaves them implicit, relying on
# Fedora's redhat-rpm-config default). Losing --wrap-mode=nodownload is
# exactly the regression #567/#580 diagnosed: without it, an unsatisfied
# BuildRequires makes meson silently fetch/vendor a subproject (e.g. pango)
# instead of failing fast. Our el10-based buildroot's redhat-rpm-config
# isn't guaranteed to default the same way Fedora's does, so both flags are
# spelled out rather than trusted to %%meson's default.
%meson \
        --wrap-mode=nodownload \
        --auto-features=enabled \
%if 0%{?with_broadway}
        -Dbroadway-backend=true \
%endif
        -Dsysprof=enabled \
%if 0%{?rhel}
        -Dmedia-gstreamer=disabled \
        -Dtracker=disabled \
%else
        -Dtracker=enabled \
%endif
        -Dcolord=enabled \
        -Ddocumentation=true \
        -Dman-pages=true \
        -Dbuild-testsuite=false \
        -Dbuild-tests=false \
        -Dbuild-examples=false

%meson_build

%install
%meson_install

%find_lang gtk40

%if !0%{?with_broadway}
rm $RPM_BUILD_ROOT%{_mandir}/man1/gtk4-broadwayd.1*
%endif

mkdir -p $RPM_BUILD_ROOT%{_sysconfdir}/gtk-4.0
mkdir -p $RPM_BUILD_ROOT%{_libdir}/gtk-4.0/modules

%check
appstream-util validate-relax --nonet $RPM_BUILD_ROOT%{_metainfodir}/*.xml
desktop-file-validate $RPM_BUILD_ROOT%{_datadir}/applications/*.desktop

%files -f gtk40.lang
%license COPYING
%doc AUTHORS NEWS README.md
%{_bindir}/gtk4-launch
%{_bindir}/gtk4-update-icon-cache
%{_libdir}/libgtk-4.so.1{,.*}
%dir %{_libdir}/gtk-4.0
%{_libdir}/gtk-4.0/modules
%{_libdir}/girepository-1.0/
%{_mandir}/man1/gtk4-launch.1*
%{_mandir}/man1/gtk4-update-icon-cache.1*
%{_datadir}/glib-2.0/schemas/org.gtk.gtk4.Inspector.gschema.xml
%{_datadir}/glib-2.0/schemas/org.gtk.gtk4.Settings.ColorChooser.gschema.xml
%{_datadir}/glib-2.0/schemas/org.gtk.gtk4.Settings.Debug.gschema.xml
%{_datadir}/glib-2.0/schemas/org.gtk.gtk4.Settings.EmojiChooser.gschema.xml
%{_datadir}/glib-2.0/schemas/org.gtk.gtk4.Settings.FileChooser.gschema.xml
%dir %{_datadir}/gtk-4.0
%{_datadir}/gtk-4.0/emoji/
%{_datadir}/mime/packages/gtk-mime.xml
%if 0%{?with_broadway}
%{_bindir}/gtk4-broadwayd
%{_mandir}/man1/gtk4-broadwayd.1*
%endif

%files devel
%{_libdir}/libgtk-4.so
%{_includedir}/*
%{_libdir}/pkgconfig/*
%{_bindir}/gtk4-builder-tool
%{_bindir}/gtk4-path-tool
%{_bindir}/gtk4-query-settings
%{_datadir}/bash-completion/completions/gtk4-builder-tool
%{_datadir}/gettext/
%{_datadir}/gir-1.0/
%{_datadir}/gtk-4.0/gtk4builder.rng
%{_datadir}/gtk-4.0/valgrind/
%{_mandir}/man1/gtk4-builder-tool.1*
%{_mandir}/man1/gtk4-path-tool.1*
%{_mandir}/man1/gtk4-query-settings.1*

%files devel-docs
%{_datadir}/doc/gdk4/
%{_datadir}/doc/gdk4-wayland/
%{_datadir}/doc/gdk4-x11/
%{_datadir}/doc/gsk4/
%{_datadir}/doc/gtk4/

%files devel-tools
%{_bindir}/gtk4-demo
%{_bindir}/gtk4-demo-application
%{_bindir}/gtk4-image-tool
%{_bindir}/gtk4-node-editor
%{_bindir}/gtk4-print-editor
%{_bindir}/gtk4-rendernode-tool
%{_bindir}/gtk4-widget-factory
%{_datadir}/applications/org.gtk.gtk4.NodeEditor.desktop
%{_datadir}/applications/org.gtk.Demo4.desktop
%{_datadir}/applications/org.gtk.PrintEditor4.desktop
%{_datadir}/applications/org.gtk.WidgetFactory4.desktop
%{_datadir}/bash-completion/completions/gtk4-demo
%{_datadir}/bash-completion/completions/gtk4-image-tool
%{_datadir}/bash-completion/completions/gtk4-node-editor
%{_datadir}/bash-completion/completions/gtk4-path-tool
%{_datadir}/bash-completion/completions/gtk4-print-editor
%{_datadir}/bash-completion/completions/gtk4-rendernode-tool
%{_datadir}/bash-completion/completions/gtk4-widget-factory
%{_datadir}/icons/hicolor/*/apps/org.gtk.gtk4.NodeEditor*.svg
%{_datadir}/icons/hicolor/*/apps/org.gtk.Demo4*.svg
%{_datadir}/icons/hicolor/*/apps/org.gtk.PrintEditor4*.svg
%{_datadir}/icons/hicolor/*/apps/org.gtk.WidgetFactory4*.svg
%{_datadir}/glib-2.0/schemas/org.gtk.Demo4.gschema.xml
%{_metainfodir}/org.gtk.gtk4.NodeEditor.appdata.xml
%{_metainfodir}/org.gtk.Demo4.appdata.xml
%{_metainfodir}/org.gtk.PrintEditor4.appdata.xml
%{_metainfodir}/org.gtk.WidgetFactory4.appdata.xml
%{_mandir}/man1/gtk4-demo.1*
%{_mandir}/man1/gtk4-demo-application.1*
%{_mandir}/man1/gtk4-image-tool.1*
%{_mandir}/man1/gtk4-node-editor.1*
%{_mandir}/man1/gtk4-rendernode-tool.1*
%{_mandir}/man1/gtk4-widget-factory.1*

%changelog
* Sat Oct 03 2026 unknown <unknown@users.noreply.github.com> - 4.24.0-1
- Update to the GNOME 51 stable release

* Fri Aug 28 2026 James Reilly <jreilly1821@gmail.com> - 4.23.3-3
- Re-fork from Fedora Rawhide's current gtk4.spec. #580's two fixes
  (pango_version floor, removed gtk4-encode-symbolic-svg/gtk4-icon-editor
  tools) are naturally inherited: Rawhide's own spec now declares
  pango_version 1.58.0 and no longer packages either tool.
- Bump cairo_version 1.18.0 -> 1.18.2, harfbuzz_version 8.4 -> 8.4.0,
  wayland_protocols_version 1.31 -> 1.48, wayland_version 1.21.0 -> 1.24.0
  to match Rawhide's current floors; these had drifted stale since this
  spec was last hand-forked.
- Drop the local libdrm BuildRequires comment: Rawhide now carries
  pkgconfig(libdrm) unconditionally (no longer behind any gate), so this
  is no longer our delta to explain.
- Define %%{gnome_major_minor_version} locally (Fedora's GNOME SIG tooling
  injects it; our buildroot doesn't have that tooling), matching the
  precedent in src/gnome-51/gobject-introspection.spec, and switch Source0
  to use it instead of a hardcoded "4.23" path component.
- Adopt Rawhide's %%meson/%%meson_build/%%meson_install in place of the
  hand-rolled meson setup/ninja invocation (a COPR-era workaround from
  4.22.1-2 that predates this repo's mock+podman build backend; proven
  unnecessary on this backend by xdg-desktop-portal's %%meson adoption
  this session), but pin --wrap-mode=nodownload and --auto-features=enabled
  explicitly rather than trust %%meson's default, since losing
  --wrap-mode=nodownload silently reopens the #567/#580 vendoring bug.
- Preserve our genuine EL10 deltas Rawhide doesn't carry: the RHEL-only
  gates (behind %%if !0%%{?rhel}) on gstreamer-player-1.0/tracker-sparql-3.0
  BuildRequires, the gstreamer1-plugins-bad-free-libs Requires, and the
  -Dmedia-gstreamer=disabled/-Dtracker=disabled %%build branch (gtk3 was
  removed on EL10 and gstreamer1-plugins-bad-free-devel still depends on
  it; tracker3/tinysparql isn't available on EL10 either).

* Wed Aug 26 2026 James Reilly <jreilly1821@gmail.com> - 4.23.3-2
- Drop 0001-gtkapplication-wayland-null-check.patch: upstream carries the
  same GDK_IS_WAYLAND_TOPLEVEL check in 4.23.3 (MR 9643 landed), so the hunk
  no longer applies and %%prep failed with "1 out of 1 hunk FAILED"
- Correct the 4.21.6-{1,2,3} changelog dates, which ascended and made rpm
  print "%%changelog not in descending chronological order" on every build

* Tue Aug 25 2026 James Reilly <jreilly1821@gmail.com> - 4.23.3-1
- Update to 4.23.3 (GNOME 51 beta cycle)

* Sat Mar 28 2026 James Reilly <jreilly1821@gmail.com> - 4.22.1-2
- Replace %%meson/%%meson_build/%%meson_install with explicit meson/ninja
  to avoid non-deterministic "fg: no job control" on COPR builders.

* Sat Mar 28 2026 James Reilly <jreilly1821@gmail.com> - 4.22.1-1
- Update to 4.22.1 (GTK stable release for GNOME 50)
- Track F44 branch instead of rawhide
- Add 0001-gtkapplication-wayland-null-check.patch (BZ 2450986)
- Drop libdrm BuildRequires (removed upstream)
- Adopt %meson build macros
- EL10: preserve gstreamer/tracker disable guards

* Mon Mar 16 2026 James Reilly <jreilly1821@gmail.com> - 4.21.6-3
- EL10: disable tracker/tinysparql integration (not available on EL10); gate
  tracker-sparql-3.0 BuildRequires and -Dtracker=enabled behind %%if !0%%{?rhel}

* Sun Mar 15 2026 James Reilly <jreilly1821@gmail.com> - 4.21.6-2
- EL10: gate gstreamer1-plugins-bad-free-devel BR and gstreamer1-plugins-bad-free-libs
  runtime Requires behind %%if !0%%{?rhel} to fix buildroot on EL10 (gtk3 was removed
  and gstreamer1-plugins-bad-free-devel still depends on libgtk-3.so.0)

* Sat Mar 14 2026 James Reilly <jreilly1821@gmail.com> - 4.21.6-1
- Initial local spec based on Fedora rawhide gtk4 4.21.6
