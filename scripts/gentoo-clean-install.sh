#!/usr/bin/env bash
# Install a gpkg into a clean Gentoo stage3 and run the recipe's checks.
# Runs INSIDE the gentoo target's image, for two callers:
#
#   verify-package-factory-cell.sh  BINHOST=/artifacts, the cell's unsigned
#                                   gpkg, before anything is published
#   publish-tideforge-gentoo.yml    BINHOST=<served URL> with KEY, the signed
#                                   binhost as a guppy image would consume it,
#                                   plus OVERLAY=<served zip>
#
# Either way the stage3 gets nothing else from TunaOS: no overlay while the
# package is installed, so it can only arrive as the binary under test, and
# its dependencies come from ::gentoo and Gentoo's official binhost.
#
# Environment:
#   ATOM      <category>/<name>
#   BINHOST   a directory of *.gpkg.tar, or an https:// binhost URL
#   KEY       (URL binhost) armored public key that signs it; signatures are
#             then REQUIRED for every binary package, Gentoo's included
#   OVERLAY   (optional) zip URL of the overlay, checked after the install
#   FILES     newline-separated paths the package must install
# Then runs /smoke.sh.
set -euo pipefail

atom=${ATOM:?}
binhost=${BINHOST:?}

emerge-webrsync --quiet
cat >> /etc/portage/make.conf <<EOF
FEATURES="-ipc-sandbox -network-sandbox -pid-sandbox"
MAKEOPTS="-j$(nproc)"
EOF

if [[ $binhost == https://* ]]; then
  # The consumer's trust path: getuto seeds /etc/portage/gnupg with Gentoo's
  # release keys under a local ultimately-trusted key; the TunaOS key gets the
  # same local signature, because Portage rejects a good signature from a key
  # it does not trust fully.
  getuto
  gpg --homedir /etc/portage/gnupg --batch --import "${KEY:?a served binhost needs KEY}"
  fingerprint=$(gpg --homedir /etc/portage/gnupg --batch --with-colons --show-keys "$KEY" \
    | awk -F: '/^fpr:/ {print $10; exit}')
  gpg --homedir /etc/portage/gnupg --batch --yes --no-tty \
    --passphrase-file /etc/portage/gnupg/pass --pinentry-mode loopback \
    --quick-lsign-key "$fingerprint"
  cat > /etc/portage/binrepos.conf/tunaos.conf <<EOF
[tunaos]
priority = 10
sync-uri = ${binhost%/}
verify-signature = true
EOF
  # FEATURES is incremental across make.conf lines.
  echo 'FEATURES="binpkg-request-signature"' >> /etc/portage/make.conf
else
  # The cell's own artifacts, as a local PKGDIR with a Packages index.
  pkgdir=$(portageq pkgdir)
  mkdir -p "$pkgdir/$atom"
  cp "$binhost"/*.gpkg.tar "$pkgdir/$atom/"
  emaint binhost --fix
fi

# The package must come from the binhost, never from a build: nothing here
# carries its ebuild, so a plan that is not a binary merge cannot be honest.
plan=$(emerge --pretend --getbinpkg --usepkg "$atom")
printf '%s\n' "$plan"
if ! grep -Eq "^\[binary[^]]*\] ${atom}-[0-9]" <<<"$plan"; then
  echo "ERROR: $atom is not planned as a binary merge from the binhost" >&2
  exit 1
fi
emerge --quiet-build --getbinpkg --usepkg --jobs="$(nproc)" "$atom"

missing=0
while IFS= read -r path; do
  [[ -n $path ]] || continue
  if [[ ! -e /$path && ! -L /$path ]]; then
    echo "ERROR: $atom did not install /$path" >&2
    missing=1
  fi
done <<<"${FILES:-}"
((missing == 0))

bash /smoke.sh

if [[ -n ${OVERLAY:-} ]]; then
  # The served overlay syncs and carries a resolvable ebuild for exactly the
  # version just installed from the binhost.
  mkdir -p /etc/portage/repos.conf
  cat > /etc/portage/repos.conf/tunaos.conf <<EOF
[tunaos]
location = /var/db/repos/tunaos
sync-type = zipfile
sync-uri = ${OVERLAY}
EOF
  emaint sync --repo tunaos
  installed=$(portageq match / "$atom")
  emerge --pretend --nodeps --usepkg=n --getbinpkg=n "=${installed}::tunaos"
fi
