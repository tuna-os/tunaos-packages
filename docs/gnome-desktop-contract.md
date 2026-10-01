# GNOME desktop experience contract

`gnome-shell` with a session file is not a complete desktop.
The session can start while file dialogs, screenshots, removable media, portals, and `keyring` credentials remain unusable.

The package repository now provides the script
[verify-gnome-desktop-experience.py](https://github.com/tuna-os/tunaos-packages/blob/main/scripts/verify-gnome-desktop-experience.py).
This check needs the core packages for session, file-manager, portal, and `keyring`:

```text
gdm
gnome-keyring
gnome-session
gnome-shell
gvfs
mutter
nautilus
xdg-desktop-portal-gnome
```

Run it after base package installation, before image publication. Examples:

```bash
rpm -qa --qf '%{NAME}\\n' | \
  python3 scripts/verify-gnome-desktop-experience.py /dev/stdin

dpkg-query -W -f '${binary:Package}\\n' | \
  python3 scripts/verify-gnome-desktop-experience.py /dev/stdin
```

The check validates names and not image size or package count.
Metapackages can expand to different package counts across openSUSE, Debian, and Ubuntu.
A size threshold can miss a missing component or reject a valid closure.
Translations for a base must use package names that the package manager emits.
Any name that the tool cannot resolve must fail the build.
