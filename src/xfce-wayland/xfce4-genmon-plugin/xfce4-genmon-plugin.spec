Name: xfce4-genmon-plugin
Version: 4.3.0
Release: 1%{?dist}
Summary: Generic monitor panel plugin for the Xfce panel
License: GPL-2.0-or-later
URL: https://gitlab.xfce.org/panel-plugins/xfce4-genmon-plugin
Source0: https://archive.xfce.org/src/panel-plugins/xfce4-genmon-plugin/4.3/xfce4-genmon-plugin-%{version}.tar.xz
BuildRequires: gcc, meson, ninja-build, gettext
BuildRequires: gtk3-devel, libxfce4ui-devel, libxfce4util-devel, xfce4-panel-devel, xfconf-devel
Requires: xfce4-panel
%description
Generic monitor panel plugin for the Xfce panel. Executes a user-defined
command periodically and displays its output in the panel.
%prep
%autosetup -n xfce4-genmon-plugin-%{version}
%build
%meson
%meson_build
%install
%meson_install
%find_lang %{name}
%files -f %{name}.lang
%license COPYING
%{_libdir}/xfce4/panel/plugins/libgenmon.so
%{_datadir}/xfce4/panel/plugins/*.desktop
%{_datadir}/icons/hicolor/*/apps/org.xfce.genmon.*
%{_datadir}/xfce4/genmon/
%changelog
* Fri Nov 20 2026 James Reilly <jreilly1821@gmail.com> - 4.3.0-1
- Update to 4.3.0 (meson). Drop the libxfce4panel header-guard patch: this
  release includes the umbrella header itself.

* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - 4.2.0-1
- Initial XFCE Wayland package for TunaOS EL10
