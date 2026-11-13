Name: xfce4-sensors-plugin
Version: 1.4.5
Release: 2%{?dist}
Summary: Sensors plugin for the Xfce panel
License: GPL-2.0-or-later
URL: https://gitlab.xfce.org/panel-plugins/xfce4-sensors-plugin
Source0: https://archive.xfce.org/src/panel-plugins/xfce4-sensors-plugin/1.4/xfce4-sensors-plugin-%{version}.tar.bz2
# Three source files include libxfce4panel/xfce-panel-plugin.h directly
# instead of the umbrella libxfce4panel.h — newer libxfce4panel enforces
# "only include the umbrella header" and errors out.
Patch0: 0001-fix-libxfce4panel-header-guard.patch
# xfce4::PluginSize/Propagation/TimeoutResponse/TooltipTime are declared
# with a deleted default constructor and brace-initialized at call sites
# ({true}/{false}). Pre-C++20 that is still aggregate init (a deleted ctor
# isn't "user-provided"); C++20 (P1008R1) redefines aggregates as having no
# user-DECLARED ctor at all, so a deleted one now disqualifies it too.
# Fedora ELN's gcc defaults to a newer -std where this is enforced; EL10/
# Fedora 44 don't hit it. Adds the single-bool constructor so the same
# brace-init keeps compiling everywhere.
Patch1: 0002-fix-cxx20-aggregate-init.patch
BuildRequires: xfce4-dev-tools
BuildRequires: autoconf automake libtool gettext-devel
# xfce4++/ is a C++ wrapper layer (.cc sources), needs g++, not just gcc.
BuildRequires: gcc-c++
BuildRequires: gtk3-devel, libxfce4ui-devel, libxfce4util-devel, xfce4-panel-devel, lm_sensors-devel, intltool, gettext, autoconf, automake, libtool
Requires: xfce4-panel, lm_sensors
%description
Sensors plugin for the Xfce panel. Displays system sensor information
such as temperatures, voltages, and fan speeds.
%prep
%autosetup -n xfce4-sensors-plugin-%{version} -p1
# The dist tarball bundles a configure script generated against an
# older libtool than EL10 ships; its nm-output parsing predates current
# binutils and mis-detects (breaks with a bogus command in the symbol
# pipe). Regenerating against the system libtool fixes it.
%build
autoreconf -fi
%configure --disable-gtk-doc
%make_build
%install
%make_install
%find_lang %{name}
%files -f %{name}.lang
%license COPYING
%{_bindir}/xfce4-sensors
%{_libdir}/xfce4/panel/plugins/libxfce4-sensors-plugin.so
%{_libdir}/xfce4/modules/libxfce4sensors.so*
%{_libdir}/pkgconfig/libxfce4sensors-1.0.pc
%{_datadir}/xfce4/panel/plugins/*.desktop
%{_datadir}/applications/*.desktop
%{_datadir}/icons/hicolor/*/apps/xfce-sensors.*
%{_datadir}/man/man1/*
%{_datadir}/xfce4/panel/plugins/xfce4-sensors-plugin.css
%changelog
* Sun Oct 25 2026 TunaOS Bot <bot@tunaos.org> - 1.4.5-2
- Add explicit single-bool constructors to the xfce4++ handler-result
  types so they still build under C++20's stricter aggregate rules
  (Fedora ELN); fixes build-0/xfce-eln-x86_64 and -aarch64.

* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - 1.4.5-1
- Initial XFCE Wayland package for TunaOS EL10
