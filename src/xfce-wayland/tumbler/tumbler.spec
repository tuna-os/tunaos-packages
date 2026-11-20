Name: tumbler
Version: 4.21.1
Release: 2%{?dist}
Summary: D-Bus thumbnailing service for Xfce
License: GPL-2.0-or-later AND LGPL-2.1-or-later
URL: https://gitlab.xfce.org/xfce/tumbler
Source0: https://archive.xfce.org/src/xfce/tumbler/4.21/tumbler-%{version}.tar.xz
BuildRequires: gcc, gtk3-devel, glib2-devel, dbus-devel, freetype-devel, libjpeg-turbo-devel, libpng-devel, libgsf-devel, intltool, gettext, meson, ninja-build
BuildRequires: libxfce4util-devel, poppler-glib-devel
%description
D-Bus thumbnailing service for the Xfce desktop environment. Provides
thumbnail generation for various file types used by file managers.

%package devel
Summary: Development files for %{name}
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
Headers and pkgconfig files for building tumbler thumbnailer plugins.

%prep
%autosetup -n tumbler-%{version}
%build
# meson's --auto-features=enabled forces every "auto" thumbnailer plugin on;
# disable the ones needing heavier/niche deps we don't otherwise want in the
# base stack (video, ebook, camera-raw), keep the common image/doc formats.
%meson -Dcover-thumbnailer=disabled -Dffmpeg-thumbnailer=disabled -Dgepub-thumbnailer=disabled -Dgst-thumbnailer=disabled -Draw-thumbnailer=disabled
%meson_build
%install
%meson_install
%find_lang %{name}

%files -f %{name}.lang
%license COPYING
%{_libdir}/tumbler-1/
%{_libdir}/libtumbler-1.so.*
%{_datadir}/dbus-1/services/*.service
%{_prefix}/lib/systemd/user/tumblerd.service
%config(noreplace) %{_sysconfdir}/xdg/tumbler/tumbler.rc
%{_datadir}/icons/hicolor/*/apps/org.xfce.tumbler.png

%files devel
%{_includedir}/tumbler-1/
%{_libdir}/libtumbler-1.so
%{_libdir}/pkgconfig/tumbler-1.pc

%changelog
* Fri Nov 20 2026 James Reilly <jreilly1821@gmail.com> - 4.21.1-2
- Build from the 4.21.1 release tarball instead of the same commit's git archive (XFCE 4.21 tag set shared with tuna-os/brussels)

* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - 4.21.1-1
- Initial XFCE Wayland package for TunaOS EL10
