Name:           thunar
Version:        4.21.7
Release:        1%{?dist}
Summary:        File manager for the Xfce desktop environment (Wayland)

License:        GPL-2.0-or-later AND LGPL-2.1-or-later
URL:            https://gitlab.xfce.org/xfce/thunar
Source0: https://archive.xfce.org/src/xfce/thunar/4.21/thunar-%{version}.tar.xz

BuildRequires: gcc
BuildRequires:  gtk3-devel
BuildRequires:  libxfce4ui-devel
BuildRequires:  libxfce4util-devel
# libxfce4session-client-1 >= 4.21.2
BuildRequires:  xfce4-session-devel >= 4.21.2
BuildRequires:  garcon-devel
BuildRequires:  xfconf-devel
BuildRequires:  libnotify-devel
BuildRequires:  intltool
BuildRequires:  gettext
BuildRequires:  meson
BuildRequires:  ninja-build
BuildRequires:  libxslt
BuildRequires:  libgudev-devel
BuildRequires:  gobject-introspection-devel
BuildRequires:  libexif-devel
# 4.21.7 has a "terminal" feature (embedded VTE terminal). %%meson turns
# auto features on, so VTE (GTK 3 API) must be in the buildroot.
BuildRequires:  vte291-devel
BuildRequires:  pcre2-devel
# xsltproc runs with --nonet; needs the docbook manpages stylesheet
# available locally via an XML catalog rewrite, not fetched over http.
BuildRequires:  docbook-style-xsl

%description
Thunar is the file manager for the Xfce desktop environment. It is designed
to be fast and easy to use. This build is compiled with X11 support disabled
(-Dx11=disabled) for use in pure Wayland sessions on TunaOS EL10.

%package devel
Summary: Development files for %{name} (thunarx plugin API)
Requires: %{name}%{?_isa} = %{version}-%{release}

%description devel
Headers and pkgconfig files for building Thunar (thunarx) plugins.

%prep
%autosetup -n thunar-%{version}

%build
# EL10's polkit (125) predates upstream polkit shipping its own gettext ITS
# ruleset (/usr/share/gettext/its/polkit.its + .loc), which msgfmt needs to
# translate org.xfce.thunar.policy.in — without it, msgfmt errors with
# "cannot locate ITS rules". Vendor the (tiny, static) upstream ruleset
# ourselves rather than depend on a base-OS package we don't control.
mkdir -p gettext-its-workaround/its
cat > gettext-its-workaround/its/polkit.its <<'ITS_EOF'
<?xml version="1.0"?>
<its:rules xmlns:its="http://www.w3.org/2005/11/its"
           version="2.0">
  <its:translateRule selector="//*" translate="no"/>
  <its:translateRule selector="//action/description |
                               //action/message"
                     translate="yes"/>
</its:rules>
ITS_EOF
cat > gettext-its-workaround/its/polkit.loc <<'LOC_EOF'
<?xml version="1.0"?>
<locatingRules>
  <locatingRule name="polkit policy" pattern="*.policy">
    <documentRule localName="policyconfig" target="polkit.its"/>
  </locatingRule>
</locatingRules>
LOC_EOF
export GETTEXTDATADIRS="$(pwd)/gettext-its-workaround${GETTEXTDATADIRS:+:${GETTEXTDATADIRS}}"

# thunar-tpa (the Thunar/xfce4-panel trash applet integration) needs
# libxfce4panel-2.0, which would make this depend on xfce4-panel — a sibling
# in the same build tier. Disable it to avoid the cross-tier ordering issue.
%meson -Dx11=disabled -Dthunar-tpa=disabled
%meson_build

%install
%meson_install
%find_lang %{name}

%post -p /sbin/ldconfig
%postun -p /sbin/ldconfig

%files -f %{name}.lang
%license COPYING
%{_bindir}/thunar
%{_bindir}/Thunar
%{_bindir}/thunar-settings
# thunar-bulk-rename is no longer a separate binary — it's now
# `thunar --bulk-rename` (see thunar-bulk-rename.desktop.in's Exec= line).
%{_libdir}/libthunar*.so.*
%{_libdir}/thunarx-3/
%{_libdir}/girepository-1.0/*.typelib
%{_libdir}/Thunar/
%{_datadir}/applications/*.desktop
%{_datadir}/dbus-1/services/*.service
%{_datadir}/polkit-1/actions/*.policy
%{_datadir}/metainfo/*.xml
%{_datadir}/icons/hicolor/*/apps/org.xfce.thunar.*
%{_datadir}/icons/hicolor/*/stock/navigation/*.png
%{_datadir}/man/man1/Thunar.1*
%{_datadir}/doc/thunar/
%{_datadir}/Thunar/
%{_datadir}/gir-1.0/*.gir
%{_sysconfdir}/xdg/Thunar/
%{_prefix}/lib/systemd/user/thunar.service

%files devel
%{_includedir}/thunarx-3/
%{_libdir}/libthunarx-3.so
%{_libdir}/pkgconfig/thunarx-3.pc

%changelog
* Fri Nov 20 2026 James Reilly <jreilly1821@gmail.com> - 4.21.7-1
- Update to the 4.21.7 release tarball (XFCE 4.21 tag set shared with tuna-os/brussels)

* Thu Jul 03 2026 TunaOS Bot <bot@tunaos.org> - 4.21.0-1
- Initial XFCE Wayland package for TunaOS EL10
