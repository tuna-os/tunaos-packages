#!/usr/bin/env bash
# Sign a wave of gpkgs into a Portage binhost and package the overlay.
# Runs INSIDE the gentoo target's image (publish-tideforge-gentoo.yml), where
# gpkg-sign, emaint and gpg all live.
#
#   --staged DIR    the wave: <category>/<name>/*.gpkg.tar, as the gate built them
#   --repo DIR      the binhost as served (synced down), updated in place
#   --overlay DIR   the rendered overlay of every recipe targeting gentoo
#   --distfiles DIR their sources under the ebuilds' SRC_URI names
#   --overlay-zip FILE  where the overlay zip is written
#
# GPG_PRIVATE_KEY (armored) and GPG_PASSPHRASE come from the environment.
set -euo pipefail

while (($#)); do
  case $1 in
    --staged) staged=$2 ;;
    --repo) repo=$2 ;;
    --overlay) overlay=$2 ;;
    --distfiles) distfiles=$2 ;;
    --overlay-zip) overlay_zip=$2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift 2
done
: "${staged:?} ${repo:?} ${overlay:?} ${distfiles:?} ${overlay_zip:?}"

mapfile -t incoming < <(cd "$staged" && find . -name '*.gpkg.tar' -printf '%P\n' | sort)
((${#incoming[@]} > 0)) || { echo "no gpkg staged" >&2; exit 1; }

export GNUPGHOME=/root/.gnupg
install -d -m 0700 "$GNUPGHOME"
printf '%s' "${GPG_PRIVATE_KEY:?}" | gpg --batch --import
key=$(gpg --batch --list-secret-keys --with-colons | awk -F: '/^fpr:/ {print $10; exit}')
install -m 0600 /dev/null /run/tunaos-gpg-pass
printf '%s' "${GPG_PASSPHRASE:-}" > /run/tunaos-gpg-pass
# Portage signs with BINPKG_GPG_SIGNING_BASE_COMMAND; the stock one expects
# an agent that already holds the passphrase, which a CI container has not.
#
# emaint then VERIFIES every signed gpkg it indexes, as an unprivileged
# user, against BINPKG_GPG_VERIFY_GPG_HOME, and accepts only a fully or
# ultimately trusted signer. So the public half goes into a keyring laid
# out the way getuto lays out /etc/portage/gnupg (root's, world-readable),
# marked ultimate: a package this job cannot verify never reaches the
# index, and the secret key stays in root's private keyring.
verify_home=/var/lib/tunaos-binpkg-verify
install -d -m 0700 "$verify_home"
gpg --batch --export "$key" | gpg --homedir "$verify_home" --batch --import
printf '%s:6:\n' "$key" | gpg --homedir "$verify_home" --batch --import-ownertrust
gpg --homedir "$verify_home" --batch --check-trustdb
gpgconf --homedir "$verify_home" --kill all
chmod u=rwx,go=rx "$verify_home"
chmod ugo+r "$verify_home/pubring.kbx" "$verify_home/trustdb.gpg"
cat >> /etc/portage/make.conf <<EOF
FEATURES="binpkg-signing"
BINPKG_GPG_SIGNING_GPG_HOME="$GNUPGHOME"
BINPKG_GPG_SIGNING_KEY="$key"
BINPKG_GPG_SIGNING_BASE_COMMAND="/usr/bin/gpg --sign --armor --batch --pinentry-mode loopback --passphrase-file /run/tunaos-gpg-pass [PORTAGE_CONFIG]"
BINPKG_GPG_VERIFY_GPG_HOME="$verify_home"
EOF

for path in "${incoming[@]}"; do
  install -D -m 0644 "$staged/$path" "$repo/$path"
  gpkg-sign "$repo/$path"
done

# The index is regenerated from every package in the tree, so it can never
# describe bytes that are not there (the createrepo --update class, #358).
# The workflow then gates it against the served one
# (check-index-regression.py --format gpkg).
rm -f "$repo/Packages" "$repo/Packages.gz"
PKGDIR=$repo emaint binhost --fix
test -s "$repo/Packages"
grep '^CPV: ' "$repo/Packages"

# The overlay: a Manifest per package from the verified sources, then one
# zip, byte-stable for identical content so an unchanged overlay keeps its
# ETag and `emaint sync` reports "not changed". Directory entries precede
# their files: Portage's zipfile sync creates directories only from those.
# `ebuild manifest` sources the ebuild, and its `inherit cargo` resolves
# through the overlay's master, ::gentoo, which the stage3 does not carry:
# without the tree the depend phase dies on "cargo.eclass could not be found
# by inherit()" (publish run 38896459570). The same signature-verified
# snapshot gentoo-build-cell.sh syncs.
emerge-webrsync --quiet
while IFS= read -r ebuild; do
  DISTDIR=$distfiles ebuild "$ebuild" manifest
done < <(find "$overlay" -name '*.ebuild' | sort)
python3 - "$overlay" "$overlay_zip" <<'PY'
import pathlib, sys, zipfile
root, destination = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
destination.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix() + ("/" if path.is_dir() else "")
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = (0o40755 if path.is_dir() else 0o100644) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, b"" if path.is_dir() else path.read_bytes())
PY
