# EL10's minimal mock root cannot parse the newer macros.gnome check.
# Keep version derivation local, as in ptyxis, and validate in prep below.
%global tarball_version %%(echo %%{version} | tr '~' '.')
%global major_version %%(echo %%{tarball_version} | cut -d "." -f 1)

Name:           orca
Version:        51.0
Release:        %autorelease
Summary:        Assistive technology for people with visual impairments

License:        LGPL-2.1-or-later AND CC-BY-SA-3.0
URL:            https://wiki.gnome.org/Projects/Orca
Source0:        https://download.gnome.org/sources/%{name}/%{major_version}/%{name}-%{tarball_version}.tar.xz


BuildArch:      noarch

BuildRequires:  pkgconfig(atk-bridge-2.0) >= 2.58.6
BuildRequires:  pkgconfig(atspi-2) >= 2.58.6
BuildRequires:  pkgconfig(gobject-introspection-1.0)
BuildRequires:  pkgconfig(liblouis)
BuildRequires:  pkgconfig(pygobject-3.0) >= 3.18
BuildRequires:  brlapi-devel
BuildRequires:  brltty
BuildRequires:  gettext
BuildRequires:  intltool
BuildRequires:  gtk3-devel
BuildRequires:  itstool
BuildRequires:  meson >= 1.0.0
# Rawhide dropped this explicit BR (the %%meson_build macro resolves ninja via
# meson's own dependency chain). We still call meson/ninja by hand below, so
# keep it explicit rather than relying on transitivity.
BuildRequires:  ninja-build
BuildRequires:  python3-brlapi
BuildRequires:  python3-dasbus
BuildRequires:  python3-devel >= 3.10
BuildRequires:  python3-louis
BuildRequires:  python3-pyatspi
BuildRequires:  python3-speechd
BuildRequires:  /usr/bin/desktop-file-validate

# Orca checks the Atspi Python override, not just the shared-library ABI.
Requires:       at-spi2-core >= 2.58.6
Requires:       at-spi2-atk >= 2.58.6
# input_event.py imports Gdk 3.0 before CLI parsing; the preferences UI also
# imports Gtk 3.0. EL10's gtk3 owns both typelibs; gtk3-devel is build-time only.
# Match meson.build's GTK 3.24 and PyGObject 3.18 floors. Keep these unqualified
# because Orca is noarch, even though the GI runtime providers are archful.
Requires:       gtk3 >= 3.24
Requires:       python3 >= 3.10
Requires:       python3-brlapi
Requires:       python3-dasbus
Requires:       python3-gobject >= 3.18
Requires:       python3-louis
Requires:       python3-pyatspi
Requires:       python3-speechd
Requires:       speech-dispatcher
# For the battery and system usage information commands
Recommends:     python3-psutil
%if 0%{?fedora}
# only needed in X11 sessions
Recommends:     libwnck3
%endif

%description
Orca is a screen reader that provides access to the graphical desktop via
user-customizable combinations of speech and/or braille. Orca works with
applications and toolkits that support the assistive technology service
provider interface (AT-SPI), e.g. the GNOME desktop.

%prep
if echo "%{version}" | grep -qE '\.(alpha|beta|rc)'; then
    echo "Error: Use tilde in Version field in front of alpha/beta/rc; checked '%{version}'" >&2
    exit 1
fi
%autosetup -p1 -n %{name}-%{tarball_version}

%build
# Expand %%meson/%%meson_build manually instead of using the macros: this
# repo's gnome-51/glib2, gnome-51/gtk4 and gnome-51/gjs specs all carry the
# identical workaround, with their changelogs attributing it to an
# intermittent "fg: no job control" failure from meson's ninja wrapper on
# COPR builders (a job-control shell builtin with no controlling tty). Not
# independently reproduced against this specific spec/pipeline, but the
# failure was non-deterministic where it was found, so one clean build here
# would not be evidence it's safe to drop -- keeping it matches every other
# meson-based spec in this tree.
#
# -Dmathcat=false: orca 51.beta added MathCAT (math speech/braille) support,
# on by default (meson_options.txt: `option('mathcat', type: 'boolean',
# value: true)`). meson.build's own check hard-errors when the option is on
# and `cargo` isn't found (find_program(required: false) then an explicit
# error()) -- we don't BuildRequire a Rust toolchain, and neither does
# Rawhide's current spec, which carries this same flag.
meson setup redhat-linux-build \
    --buildtype=plain \
    --prefix=%{_prefix} \
    --libdir=%{_libdir} \
    --sysconfdir=%{_sysconfdir} \
    --localstatedir=%{_localstatedir} \
    --wrap-mode=nodownload \
    -Ddefault_library=shared \
    -Dmathcat=false
meson compile -C redhat-linux-build

%install
meson install -C redhat-linux-build --no-rebuild --destdir=%{buildroot}
%find_lang %{name} --with-gnome

%check
desktop-file-validate %{buildroot}%{_sysconfdir}/xdg/autostart/orca-autostart.desktop

%files -f %{name}.lang
%license COPYING
%doc AUTHORS NEWS README.md
%{_bindir}/orca
%{python3_sitelib}/orca/
%{_datadir}/icons/hicolor/*/apps/orca.png
%{_datadir}/icons/hicolor/scalable/apps/orca.svg
%{_datadir}/icons/hicolor/symbolic/apps/orca-symbolic.svg
%{_datadir}/applications/orca.desktop
%{_datadir}/glib-2.0/schemas/org.gnome.Orca.gschema.xml
%{_sysconfdir}/xdg/autostart/orca-autostart.desktop
%{_mandir}/man1/orca.1*
%{_userunitdir}/orca.service

%changelog
%autochangelog
