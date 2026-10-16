#!/usr/bin/env bash
# Build one Tideforge recipe into a gpkg INSIDE the gentoo target's stage3.
# Called by run-package-factory-cell.sh (the gpkg branch) with:
#
#   /work/overlay    the overlay `tideforge.py render --target gentoo` wrote;
#                    the Manifest is generated here
#   /work/distfiles  the recipe's sources, sha256-verified by the fetch and
#                    named as the ebuild's SRC_URI renames them
#   /work/artifacts  where the built gpkg lands
#
# and ATOM=<category>/<name>, the one package the overlay carries.
set -euo pipefail

atom=${ATOM:?}
jobs=$(nproc)

# A signature-verified ::gentoo snapshot (emerge-webrsync checks it against
# the release keys the stage3 ships). The stage3 carries no tree.
emerge-webrsync --quiet

# The ipc/network/pid sandboxes need namespaces an unprivileged container
# does not grant (guppy's Containerfile.gentoo turns them off too). Gentoo's
# official binhost is already in the stage3's binrepos.conf, with
# verify-signature = true, and supplies the prebuilt dependencies.
cat >> /etc/portage/make.conf <<EOF
FEATURES="-ipc-sandbox -network-sandbox -pid-sandbox"
MAKEOPTS="-j${jobs}"
EOF

mkdir -p /etc/portage/repos.conf
cat > /etc/portage/repos.conf/tunaos.conf <<'EOF'
[tunaos]
location = /work/overlay
EOF

# Portage verifies every distfile against the Manifest before unpacking, and
# writes it from the files it is given: these are the bytes the recipe's
# sha256 already pinned, so the BLAKE2B/SHA512 lines describe the same
# archives.
ebuild_path=$(find "/work/overlay/${atom}" -name '*.ebuild' -print -quit)
DISTDIR=/work/distfiles ebuild "$ebuild_path" manifest

# Dependencies first, from the binhost where it has a build for the
# profile's USE and from source where it does not (gtk4-layer-shell, for
# one). Keep respect-use enabled, like guppy's desktop stage (its base
# bootstrap alone disables it): otherwise this stage3 takes abi_x86_32 and
# elogind builds that cannot resolve against the installed systemd world.
emerge --quiet-build --getbinpkg --jobs="$jobs" \
  --load-average="$jobs" --onlydeps --with-bdeps=y "$atom::tunaos"

# The package itself, from source and only into /work/binpkgs: --getbinpkg=n
# and --usepkg=n keep a binary of the same name from substituting for the
# build under test.
mkdir -p /work/binpkgs
DISTDIR=/work/distfiles PKGDIR=/work/binpkgs emerge --quiet-build=n --oneshot \
  --getbinpkg=n --usepkg=n --buildpkg "$atom::tunaos"

find /work/binpkgs -name '*.gpkg.tar' -exec cp -t /work/artifacts {} +
