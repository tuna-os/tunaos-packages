Name:           glib2
Version:        2.90.0
# EL10: manual Release, not %%autorelease. Two reasons:
#  1. rpmautospec macros aren't guaranteed in our buildroot.
#  2. The full build's Release must stay ahead of glib2-bootstrap.spec's
#     (currently 1%%{?dist}). build-chain skips a package whose exact NVR
#     it has already built, so if the full build ever lands on the same
#     NVR as the bootstrap, it gets silently skipped and the stub
#     bootstrap glib2-devel -- which ships no gir files -- stays in the
#     buildroot. Every introspection-generating package then fails on
#     "Couldn't find include 'GObject-2.0.gir'". Keep this > bootstrap's.
Release:        2%{?dist}
Summary:        A library of handy utility functions

License:        LGPL-2.1-or-later
URL:            https://www.gtk.org
# Rawhide spells this with %%{gnome_major_minor_version}; that macro isn't
# defined in our buildroot (not shipped by any package we pull in), so we
# keep the version component literal.
Source0:        https://download.gnome.org/sources/glib/2.90/glib-%{version}.tar.xz

# Required for RHEL core crypto components policy. Good for Fedora too.
# https://bugzilla.redhat.com/show_bug.cgi?id=1630260
# https://gitlab.gnome.org/GNOME/glib/-/merge_requests/903
Patch0:         gnutls-hmac.patch

# https://bugzilla.redhat.com/show_bug.cgi?id=2192204
Patch1:         default-terminal.patch

BuildRequires:  gcc
BuildRequires:  gcc-c++
BuildRequires:  gettext
BuildRequires:  perl-interpreter
BuildRequires:  glibc-devel
BuildRequires:  libattr-devel
BuildRequires:  libselinux-devel
BuildRequires:  meson
BuildRequires:  systemtap-sdt-devel
BuildRequires:  systemtap-sdt-dtrace
BuildRequires:  pkgconfig(libelf)
BuildRequires:  pkgconfig(libffi)
BuildRequires:  pkgconfig(libpcre2-8)
BuildRequires:  pkgconfig(mount)
BuildRequires:  pkgconfig(sysprof-capture-4)
BuildRequires:  pkgconfig(zlib)
BuildRequires:  python3-devel
# rst2man generator for the man pages built below. Rawhide pulls this in via
# the file dependency /usr/bin/rst2man; we BuildRequire the package directly
# since that's what actually provides it in our buildroot.
BuildRequires:  python3-docutils
# EL10's stock gobject-introspection-devel is 1.79.1 (verified against the
# c10s-build koji repo, 2026-08-28) and cannot be relied on for the
# GIRepository-3.0 / gi-compile-repository toolchain glib2 now ships. This
# floor is satisfied by our own gnome-51 gobject-introspection build, not
# by CentOS Stream's stock package -- keep it.
BuildRequires:  gobject-introspection-devel >= 1.80.0

# For gnutls-hmac.patch. We now dlopen libgnutls.so.30 so that we can build a
# static glib2 without depending on a static build of GnuTLS as well. This will
# ensure we notice if the GnuTLS soname bumps, so that we can update our patch.
BuildRequires:  gnutls
%if 0%{?__isa_bits} == 64
Requires: libgnutls.so.30()(64bit)
%else
Requires: libgnutls.so.30
%endif

Provides: bundled(cmph)
Provides: bundled(dirent)
Provides: bundled(gnulib)
Provides: bundled(gvdb)
Provides: bundled(libcharset)
Provides: bundled(xdgmime)

# glib typelib files moved from gobject-introspection to glib2 in F40
Conflicts: gobject-introspection < 1.79.1

%description
GLib is the low-level core library that forms the basis for projects
such as GTK+ and GNOME. It provides data structure handling for C,
portability wrappers, and interfaces for such runtime functionality
as an event loop, threads, dynamic loading, and an object system.

%package devel
Summary: A library of handy utility functions
Requires: %{name}%{?_isa} = %{version}-%{release}
# Without this, downstream BuildRequires on glib2-devel no longer pull in
# glibc-devel transitively, causing "C compiler cannot create executables"
# in every chroot (all arches). Rawhide doesn't need this because Fedora's
# gcc/glibc BuildRequires chain already guarantees it; ours doesn't, so
# keep it explicit. (2.88.0-4)
Requires: glibc-devel
# Required by gdbus-codegen
Requires: python3-packaging
# glib gir files moved from gobject-introspection-devel to glib2-devel in F40
Conflicts: gobject-introspection-devel < 1.79.1

%description devel
The glib2-devel package includes the header files for the GLib library.

%package static
Summary: glib static
Requires: %{name}-devel = %{version}-%{release}
Requires: pcre2-static
Requires: sysprof-capture-static

%description static
The %{name}-static subpackage contains static libraries for %{name}.

%prep
%autosetup -n glib-%{version} -p1
# Patch frexp checks for mock environment
python3 -c "
import re
path = 'glib/gnulib/meson.build'
content = open(path).read()
content = re.sub(r'if not have_frexp.*?endif', 'have_frexp = true\ngl_cv_func_frexp_works = true', content, flags=re.DOTALL)
content = re.sub(r'if not have_frexpl.*?endif', 'have_frexpl = true\ngl_cv_func_frexpl_works = true\ngl_cv_func_frexpl_decl = true', content, flags=re.DOTALL)
with open(path, 'w') as f: f.write(content)
"

%build
# Explicit meson/ninja invocation instead of the %%meson/%%meson_build macros:
# those macros end with `fg` to bring ninja to the foreground, which fails
# with "fg: no job control" when rpmbuild runs under a non-interactive shell
# (observed on COPR builders using --console=pipe; our CI runs rpmbuild the
# same non-interactive way). (2.88.0-3)
#
# documentation=false / installed_tests=false: gi-docgen is present in EL10's
# c10s-build repo (verified 2026-08-28, gi-docgen-2023.3-10.el10), so this is
# no longer a hard buildroot gap -- but wiring up glib2-doc/glib2-tests and a
# %%check that exercises glib's own suite is scope beyond this drift-fix pass
# and nothing in this package set consumes glib2-doc. Left off deliberately;
# revisit as its own change if a consumer needs it.
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
    --auto-features=enabled \
    --wrap-mode=nodownload \
    -Dglib_debug=disabled \
    -Ddocumentation=false \
    -Dinstalled_tests=false \
    -Dgnutls=true \
    -Dintrospection=enabled \
    --default-library=both
ninja -C _build -j%{_smp_build_ncpus}

%install
DESTDIR=%{buildroot} ninja -C _build install

# Perform byte compilation manually on paths outside the usual locations
%py_byte_compile %{python3} %{buildroot}%{_datadir}

mv %{buildroot}%{_bindir}/gio-querymodules %{buildroot}%{_bindir}/gio-querymodules-%{__isa_bits}
sed -i -e "/^gio_querymodules=/s/gio-querymodules/gio-querymodules-%{__isa_bits}/" %{buildroot}%{_libdir}/pkgconfig/gio-2.0.pc

mkdir -p %{buildroot}%{_libdir}/gio/modules
touch %{buildroot}%{_libdir}/gio/modules/giomodule.cache

%find_lang glib20

%transfiletriggerin -- %{_libdir}/gio/modules
gio-querymodules-%{__isa_bits} %{_libdir}/gio/modules &> /dev/null || :

%transfiletriggerpostun -- %{_libdir}/gio/modules
gio-querymodules-%{__isa_bits} %{_libdir}/gio/modules &> /dev/null || :

%transfiletriggerin -- %{_datadir}/glib-2.0/schemas
glib-compile-schemas %{_datadir}/glib-2.0/schemas &> /dev/null || :

%transfiletriggerpostun -- %{_datadir}/glib-2.0/schemas
glib-compile-schemas %{_datadir}/glib-2.0/schemas &> /dev/null || :

%files -f glib20.lang
%license LICENSES/LGPL-2.1-or-later.txt
%doc NEWS README.md
%{_libdir}/libglib-2.0.so.0*
%{_libdir}/libgthread-2.0.so.0*
%{_libdir}/libgmodule-2.0.so.0*
%{_libdir}/libgobject-2.0.so.0*
%{_libdir}/libgio-2.0.so.0*
%{_libdir}/libgirepository-2.0.so.0*
%dir %{_libdir}/girepository-1.0
%{_libdir}/girepository-1.0/*.typelib
%dir %{_datadir}/bash-completion
%dir %{bash_completions_dir}
%{bash_completions_dir}/gapplication
%{bash_completions_dir}/gdbus
%{bash_completions_dir}/gio
%{bash_completions_dir}/gsettings
%dir %{_datadir}/glib-2.0
%dir %{_datadir}/glib-2.0/schemas
%dir %{_libdir}/gio
%dir %{_libdir}/gio/modules
%ghost %{_libdir}/gio/modules/giomodule.cache
%{_bindir}/gio
%{_bindir}/gio-querymodules*
%{_bindir}/glib-compile-schemas
%{_bindir}/gsettings
%{_bindir}/gdbus
%{_bindir}/gapplication
%{_libexecdir}/gio-launch-desktop
%{_mandir}/man1/gio.1*
%{_mandir}/man1/gio-querymodules.1*
%{_mandir}/man1/glib-compile-schemas.1*
%{_mandir}/man1/gsettings.1*
%{_mandir}/man1/gdbus.1*
%{_mandir}/man1/gapplication.1*

%files devel
%{_libdir}/lib*.so
%{_libdir}/glib-2.0
%{_includedir}/gio-unix-2.0/
%{_includedir}/glib-2.0/
%{_datadir}/aclocal/*
%{_libdir}/pkgconfig/*
%{_datadir}/glib-2.0/dtds
%{_datadir}/glib-2.0/gdb
%{_datadir}/glib-2.0/gettext
%{_datadir}/glib-2.0/schemas/gschema.dtd
%dir %{_datadir}/glib-2.0/valgrind
%{_datadir}/glib-2.0/valgrind/glib.supp
%{bash_completions_dir}/gresource
%{_bindir}/glib-genmarshal
%{_bindir}/glib-gettextize
%{_bindir}/glib-mkenums
%{_bindir}/gi-compile-repository
%{_bindir}/gi-decompile-typelib
%{_bindir}/gi-inspect-typelib
%{_bindir}/gobject-query
%{_bindir}/gtester
%{_bindir}/gdbus-codegen
%{_bindir}/glib-compile-resources
%{_bindir}/gresource
%{_datadir}/glib-2.0/codegen
%attr (0755, root, root) %{_bindir}/gtester-report
%{_mandir}/man1/glib-genmarshal.1*
%{_mandir}/man1/glib-gettextize.1*
%{_mandir}/man1/glib-mkenums.1*
%{_mandir}/man1/gi-compile-repository.1*
%{_mandir}/man1/gi-decompile-typelib.1*
%{_mandir}/man1/gi-inspect-typelib.1*
%{_mandir}/man1/gobject-query.1*
%{_mandir}/man1/gtester-report.1*
%{_mandir}/man1/gtester.1*
%{_mandir}/man1/gdbus-codegen.1*
%{_mandir}/man1/glib-compile-resources.1*
%{_mandir}/man1/gresource.1*
%{_datadir}/gdb/
%dir %{_datadir}/gir-1.0
%{_datadir}/gir-1.0/*.gir
%{_datadir}/gettext/
%{_datadir}/systemtap/

%files static
%{_libdir}/libgio-2.0.a
%{_libdir}/libgirepository-2.0.a
%{_libdir}/libglib-2.0.a
%{_libdir}/libgmodule-2.0.a
%{_libdir}/libgobject-2.0.a
%{_libdir}/libgthread-2.0.a

%changelog
* Sat Oct 03 2026 unknown <unknown@users.noreply.github.com> - 2.90.0-2
- Update to the GNOME 51 stable release

* Fri Aug 28 2026 James Reilly <jreilly1821@gmail.com> - 2.89.4-3
- Re-fork against Fedora Rawhide's current glib2.spec (fetched
  2026-08-28) to stop this spec from drifting silently. No version or
  patch change (Rawhide is also at 2.89.4 with the same two patches,
  gnutls-hmac.patch and default-terminal.patch -- neither is a drop
  candidate).
- Fix a real bug found in the diff: `Conflicts: gobject-introspection-devel
  < 1.79.1` was in the main package's preamble instead of %%package devel,
  so glib2-devel never actually carried it. Split placement to match
  Rawhide: the gobject-introspection Conflicts stays on the main package
  (owns the moved typelib files), the gobject-introspection-devel Conflicts
  moves to %%package devel (owns the moved gir files).
- Move gi-compile-repository/gi-decompile-typelib/gi-inspect-typelib (and
  their man pages) from %%files main to %%files devel, matching Rawhide's
  current split. These are introspection dev tools, not runtime tools.
- Adopt Rawhide's arch-conditional `Requires: libgnutls.so.30()(64bit)`
  instead of `gnutls%%{?_isa}`. The 2.88.0-2 switch away from this form
  was because the bare soname Requires lacked the ()(64bit) qualifier at
  the time; Rawhide's current spec already carries the qualifier, so the
  original problem this worked around no longer exists upstream. Verified
  the built RPM's Requires after the local build.
- Adopt Rawhide's `pcre2-static` + `sysprof-capture-static` Requires on
  glib2-static, replacing the old `pcre2-devel` floor. Both static
  packages exist in EL10's c10s-build repo (checked directly against its
  primary.xml, 2026-08-28: pcre2-static-10.44-1.el10.3,
  sysprof-capture-static-47.2-1.el10) -- this was stale, not a genuine
  EL10 gap.
- Drop the unreferenced glib-do-not-install-localtime-test.patch file;
  it hasn't been in the Patch: list since 2.88.0-1.
- Everything else EL10/hummingbird-specific is kept as-is and re-verified
  against the live c10s-build repo rather than assumed: glibc-devel
  Requires on -devel, the gobject-introspection-devel >= 1.80.0 build
  floor (repo still only ships 1.79.1; our own gnome-51 gobject-
  introspection build satisfies the floor), the explicit meson/ninja
  invocation instead of the %%meson macros (job-control failure under
  non-interactive rpmbuild), and documentation=false/installed_tests=false
  (gi-docgen is now present in EL10 but wiring up glib2-doc/glib2-tests
  is out of scope for this drift-fix pass).

* Tue Aug 25 2026 James Reilly <jreilly1821@gmail.com> - 2.89.4-2
- Keep the full build's release ahead of glib2-bootstrap's. The chain skips a
  package whose exact NVR it already built, so a full build sharing the
  bootstrap's NVR is silently skipped and the stub bootstrap glib2-devel --
  which ships no gir files -- stays in the buildroot. Every introspection
  generating package then fails on "Couldn't find include 'GObject-2.0.gir'".

* Tue Aug 25 2026 James Reilly <jreilly1821@gmail.com> - 2.89.4-1
- Update to 2.89.4 (GNOME 51 beta cycle)

* Mon Apr 07 2026 James Reilly <jreilly1821@gmail.com> - 2.88.0-4
- Add Requires: glibc-devel to glib2-devel; without it downstream BuildRequires
  on glib2-devel no longer pulled in glibc-devel transitively, causing
  "C compiler cannot create executables" in all chroots (all arches).

* Sat Mar 28 2026 James Reilly <jreilly1821@gmail.com> - 2.88.0-3
- Replace %%meson/%%meson_build/%%meson_install macros with explicit meson setup,
  ninja, and DESTDIR install to avoid "fg: no job control" failure on COPR
  builders that run rpmbuild under --console=pipe (non-interactive bash).

* Sat Mar 28 2026 James Reilly <jreilly1821@gmail.com> - 2.88.0-2
- Fix runtime Requires: use gnutls%%{?_isa} instead of arch-conditional
  libgnutls.so.30 soname form; the bare soname without the ()(64bit) qualifier
  is unresolvable on aarch64, causing DNF to fall back to glib2-2.87.3-1 which
  has pkgconfig files in the main package and no glib2-devel split.

* Sat Mar 28 2026 James Reilly <jreilly1821@gmail.com> - 2.88.0-1
- Update to 2.88.0 (GNOME 50 stable release)
- Track F44 branch instead of rawhide
- Add gobject-introspection Conflicts for correctness
- Add explicit gnutls BR and runtime Requires
- Fix gio-querymodules sed to be arch-agnostic
- Add gio modules cache dir and ghost entry
- Drop glib-do-not-install-localtime-test.patch (not needed with installed_tests=false)
- Adopt %meson macros for build/install
- EL10: keep documentation=false, installed_tests=false (gi-docgen unavailable)
- EL10: exclude doc and tests subpackages
- Add python3-docutils BR for rst2man (man page generation, available in EL10 CRB)
- Add version floor >= 1.80.0 on gobject-introspection-devel BR
- Add man pages to %%files main and %%files devel (new in 2.88.0)
- Add GDB auto-load __pycache__ dir to %%files devel

* Fri Mar 13 2026 Conductor <james@conductor.local> - 2.87.3-2
- Add transfiletrigger scriptlets for glib-compile-schemas and gio-querymodules
  to match Rawhide and fix missing schema compilation on EL10

* Thu Mar 12 2026 Conductor <james@conductor.local> - 2.87.3-1
- Final clean build with introspection enabled and missing files included
