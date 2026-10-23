Name:           libglib-testing
Version:        0.1.0
Release:        5%{?dist}
Summary:        GLib-based test library and harness

License:        LGPL-2.1-or-later
URL:            https://gitlab.gnome.org/pwithnall/libglib-testing
Source0:        https://gitlab.gnome.org/pwithnall/libglib-testing/-/archive/%{version}/%{name}-%{version}.tar.gz

BuildRequires:  meson
BuildRequires:  gcc
BuildRequires:  pkgconfig(gio-2.0)

%description
libglib-testing is a test library providing test harnesses and mock classes
which complement the classes provided by GLib. It is intended to be used by
any project which uses GLib and which wants to write internal unit tests.

%package devel
Summary:        Development files for %{name}
License:        LGPL-2.1-or-later
Requires:       %{name}%{?_isa} = %{version}-%{release}

%description devel
This package contains the pkg-config file and development headers
for %{name}.

%prep
%autosetup -n %{name}-%{version}
# Remove docs subdir — gtkdoc-scan not available in EL10 buildroot
sed -i "/^subdir('docs')/d" libglib-testing/meson.build

%build
meson setup _build \
    --buildtype=plain \
    --prefix=%{_prefix} \
    --libdir=%{_libdir} \
    --libexecdir=%{_libexecdir} \
    --includedir=%{_includedir} \
    --datadir=%{_datadir} \
    --wrap-mode=nodownload
ninja -C _build -j%{_smp_build_ncpus}

%install
DESTDIR=%{buildroot} ninja -C _build install

%files
%license COPYING
%{_libdir}/libglib-testing-0.so.*

%files devel
%{_includedir}/glib-testing-0/
%{_libdir}/libglib-testing-0.so
%{_libdir}/pkgconfig/glib-testing-0.pc

%changelog
* Sun Oct 18 2026 TunaOS Package Factory <packages@tunaos.org> - 0.1.0-5
- Use GitLab's gzip archive endpoint; the bzip2 endpoint returns HTTP 406 in
  the package factory downloader.

* Sun Aug 23 2026 TunaOS Package Factory <packages@tunaos.org> - 0.1.0-4
- Use the SPDX identifier LGPL-2.1-or-later. LicenseRef-Callaway-LGPLv2+ is
  not a valid LicenseRef: SPDX allows only [A-Za-z0-9.-] in the idstring, so
  the trailing + made it invalid and rpmlint failed the cell (#480)

* Mon Mar 30 2026 James Reilly <jreilly1821@gmail.com> - 0.1.0-3
- Fix %%files devel: include dir is glib-testing-0/ not libglib-testing-0/

* Mon Mar 30 2026 James Reilly <jreilly1821@gmail.com> - 0.1.0-2
- Remove docs subdir: gtkdoc-scan not available in EL10 buildroot

* Mon Mar 30 2026 James Reilly <jreilly1821@gmail.com> - 0.1.0-1
- Initial package for EL10; build dep for malcontent parental controls
