Name: xfce4-dev-tools
Version: 4.21.2
Release: 1%{?dist}
Summary: Xfce developer tools (xdt-autogen, m4 macros)
License: GPL-2.0-or-later
URL: https://docs.xfce.org/xfce/xfce4-dev-tools/start
Source0: https://archive.xfce.org/src/xfce/xfce4-dev-tools/4.21/xfce4-dev-tools-%{version}.tar.bz2

BuildRequires: gcc
BuildRequires: make
BuildRequires: glib2-devel
BuildRequires: libxslt
BuildRequires: meson
BuildRequires: ninja-build
BuildRequires: autoconf automake libtool gettext-devel
Requires: autoconf automake libtool gettext-devel

%description
Autotools helper scripts and m4 macros (xdt-autogen) required to bootstrap
Xfce components built from git snapshots rather than dist tarballs.

%prep
%autosetup -n xfce4-dev-tools-%{version}

# Same class of bug as the other release-tarball specs: the bundled
# configure predates EL10's binutils nm-output format and mis-parses it.
%build
autoreconf -fi
%configure
%make_build

%install
%make_install

%files
%license COPYING
%{_bindir}/xdt-*
%{_bindir}/xfce-*
%{_datadir}/aclocal/*.m4
%{_mandir}/man1/xdt-csource.1*

%changelog
* Fri Nov 20 2026 James Reilly <jreilly1821@gmail.com> - 4.21.2-1
- Update to the 4.21.2 release tarball (XFCE 4.21 tag set shared with tuna-os/brussels)

* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - 4.20.0-1
- Initial package: build-time bootstrap for git-snapshot Xfce components
