Name: xfce4-appfinder
Version: 4.21.1
Release: 2%{?dist}
Summary: Application finder for the Xfce desktop environment
License: GPL-2.0-or-later
URL: https://gitlab.xfce.org/xfce/xfce4-appfinder
Source0: https://archive.xfce.org/src/xfce/xfce4-appfinder/4.21/xfce4-appfinder-%{version}.tar.xz

BuildRequires: gcc
BuildRequires: gtk3-devel
BuildRequires: libxfce4ui-devel
BuildRequires: libxfce4util-devel
BuildRequires: garcon-devel
BuildRequires: intltool
BuildRequires: gettext
BuildRequires: meson
BuildRequires: ninja-build

Requires: garcon

%description
Application finder for the Xfce desktop environment.

%prep
%autosetup -n xfce4-appfinder-%{version}

%build
%meson
%meson_build

%install
%meson_install
%find_lang %{name}

%files -f %{name}.lang
%license COPYING
%{_bindir}/xfce4-appfinder
# xfce4-run was renamed upstream — it's now xfrun4, a symlink to xfce4-appfinder.
%{_bindir}/xfrun4
%{_datadir}/applications/*.desktop
%{_datadir}/metainfo/*.xml
%{_datadir}/icons/hicolor/*/apps/org.xfce.appfinder.*

%changelog
* Fri Nov 20 2026 James Reilly <jreilly1821@gmail.com> - 4.21.1-2
- Build from the 4.21.1 release tarball instead of the same commit's git archive (XFCE 4.21 tag set shared with tuna-os/brussels)

* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - 4.21.1-1
- Initial XFCE Wayland package for TunaOS EL10
