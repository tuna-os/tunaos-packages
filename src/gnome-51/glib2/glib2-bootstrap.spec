Name:           glib2
Version:        2.90.0
Release:        1%{?dist}
Summary:        A library of handy utility functions
License:        LGPL-2.1-or-later
URL:            https://www.gtk.org
Source0:        https://download.gnome.org/sources/glib/2.90/glib-%{version}.tar.xz

BuildRequires:  meson
BuildRequires:  gcc
BuildRequires:  gcc-c++
BuildRequires:  gettext
BuildRequires:  pkgconfig(libpcre2-8)
BuildRequires:  pkgconfig(libffi)
BuildRequires:  pkgconfig(zlib)
BuildRequires:  pkgconfig(mount)
BuildRequires:  python3-devel
BuildRequires:  python3-docutils

Obsoletes:      glib2 < %{version}
Provides:       glib2 = %{version}-%{release}

%description
GLib is the low-level core library that forms the basis for projects such as
GNOME and GTK.

%package devel
Summary:        Development files for glib2
Requires:       %{name}%{?_isa} = %{version}-%{release}
Obsoletes:      glib2-devel < %{version}
Provides:       glib2-devel = %{version}-%{release}

%description devel
Development files for glib2.

%prep
%autosetup -n glib-%{version}
# Patch frexp checks
python3 -c "
import re
path = 'glib/gnulib/meson.build'
content = open(path).read()
content = re.sub(r'if not have_frexp.*?endif', 'have_frexp = true\ngl_cv_func_frexp_works = true', content, flags=re.DOTALL)
content = re.sub(r'if not have_frexpl.*?endif', 'have_frexpl = true\ngl_cv_func_frexpl_works = true\ngl_cv_func_frexpl_decl = true', content, flags=re.DOTALL)
with open(path, 'w') as f: f.write(content)
"

%build
meson setup --prefix=/usr --libdir=/usr/lib64 --buildtype=plain build \
    -Dglib_debug=disabled \
    -Ddocumentation=false \
    -Dinstalled_tests=false \
    -Dintrospection=disabled
meson compile -C build

%install
DESTDIR=%{buildroot} meson install -C build

%files
/usr/bin/*
/usr/lib64/*.so*
/usr/lib64/pkgconfig/*.pc
/usr/include/*
/usr/share/locale/*/LC_MESSAGES/*.mo
/usr/share/glib-2.0/*
/usr/lib64/glib-2.0/include/*
/usr/libexec/gio-launch-desktop
/usr/share/aclocal/*.m4
/usr/share/bash-completion/completions/*
/usr/share/gdb/auto-load/usr/lib64/*.py
/usr/share/gettext/its/gschema.*

%files devel
# For now combined

%changelog
* Sat Oct 03 2026 unknown <unknown@users.noreply.github.com> - 2.90.0-1
- Update to the GNOME 51 stable release

* Tue Aug 25 2026 James Reilly <jreilly1821@gmail.com> - 2.89.4-1
- Update to 2.89.4 (GNOME 51 beta cycle)

* Sat Mar 28 2026 James Reilly <jreilly1821@gmail.com> - 2.88.0-1
- Update to 2.88.0 (GNOME 50 stable release)
- Track F44 branch

* Thu Mar 12 2026 Conductor <james@conductor.local> - 2.87.3-1
- Bootstrap upgrade build
