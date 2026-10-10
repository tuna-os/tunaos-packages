#!/usr/bin/env bash
# Clean-install a generated Arch package from an ephemeral repository.
#
# Usage: arch-clean-install.sh <package> <artifact-dir> [smoke-command...]
#
# This lives in a file rather than inline in the workflow because the inline
# version was silently not running. It built an ephemeral repository with a
# heredoc:
#
#     docker run ... bash -lc '
#       ...
#       cat > /tmp/tideforge-pacman.conf <<EOF
#       [options]
#       ...
#       EOF
#       pacman --config /tmp/tideforge-pacman.conf -S --noconfirm $package
#       pacman -Q $package
#     '
#
# Two independent defects, either of which is fatal:
#
#   1. The terminator was indented. An unquoted heredoc terminator has to be at
#      column 0 (only <<- strips leading TABS, and YAML block scalars indent
#      with spaces), so it never matched and the heredoc ran to end of script.
#   2. The delimiter was written <<'EOF' inside a single-quoted bash -lc string.
#      Those quotes close the outer string early — the same landmine already
#      documented in this workflow at 7af9ae1.
#
# The result: every command after the heredoc was swallowed as heredoc body.
# The package was never installed, `pacman -Q` never ran, and the job still
# went green, because the last command that actually executed (repo-add)
# succeeded and bash only *warns* about an unterminated heredoc. Run
# 30592649130 shows all nine matrix entries passing this way.
#
# So the "clean install" gate asserted nothing at all. Keeping the script in a
# file removes both quoting hazards: no nesting, no heredoc.
set -euo pipefail

package="${1:?usage: arch-clean-install.sh <package> <artifact-dir> [smoke...]}"
artifacts="${2:?usage: arch-clean-install.sh <package> <artifact-dir> [smoke...]}"
shift 2

# Same mirror pin as the build container (build-tideforge-arch.yml): on
# 2026-08-18 fastly served a core.db naming elfutils-0.195-8 while every
# pool 404d it, so any sync that takes fastly's db resolves packages no
# mirror still carries. One mirror keeps db and pool in step.
policy="$(dirname "$0")/arch-native-policy.sh"
bash "$policy" configure "${TUNAOS_ARCHITECTURE:-$(uname -m)}"
native_repositories=$(bash "$policy" repositories)
pacman -Sy --noconfirm pacman-contrib pkgconf

repo-add /tmp/tideforge.db.tar.gz "$artifacts"/*.pkg.tar.*
mkdir -p /var/lib/tideforge
cp "$artifacts"/*.pkg.tar.* /var/lib/tideforge/
# pacman resolves a repository named "tideforge" as tideforge.db. repo-add
# writes a compressed archive, so expose it under the name the file://
# repository client actually requests.
cp /tmp/tideforge.db.tar.gz /var/lib/tideforge/tideforge.db

# CI artifacts are deliberately unsigned. Production promotion has a separate
# signing gate; this isolated repository exists solely to prove pacman's
# dependency solver and payload installation.
#
# Keep the repository definition out of /etc: hosted container engines may
# user-map root and make image-owned files read-only, and recent Arch images
# no longer guarantee a copied /etc/pacman.conf survives that mapping. Write a
# small, complete configuration of our own instead.
#
# [tideforge] MUST come before [core] and [extra]. pacman resolves `-S <name>`
# by walking the sync repositories in configuration order and taking the first
# one that provides the name — it does not compare versions across them. With
# tideforge listed last, every package name that also exists in an official
# Arch repository was installed FROM Arch, and the artifact this job just built
# was never exercised at all.
#
# That is not hypothetical. Run 31113235209 built bazaar 0.9.1-1, uploaded it as
# the job's artifact, and then reported `bazaar 0.9.2-1` from `pacman -Q` —
# extra's build, not ours. It only surfaced because Arch's 0.9.2 replaced
# /usr/bin/bazaar-refresh-worker with bazaar-daemon and tripped the smoke. The
# other matrix entries that exist in extra (niri, greetd, dgop) stayed green
# while validating a package this repository did not produce, which is the more
# expensive failure: a gate that passes for the wrong reason teaches nothing.
#
# Ordering it first is what makes the closure check and the smoke assertions
# statements about OUR package. tideforge holds only what this job built, so
# it can shadow nothing else.
{
    echo '[options]'
    echo 'Architecture = auto'
    echo 'SigLevel = Required DatabaseOptional'
    echo 'LocalFileSigLevel = Optional'
    echo
    echo '[tideforge]'
    echo 'SigLevel = Optional TrustAll'
    echo 'Server = file:///var/lib/tideforge'
    echo
    while IFS= read -r native_repo; do
        printf '[%s]\n' "$native_repo"
        echo 'SigLevel = Required DatabaseOptional'
        echo 'Include = /etc/pacman.d/mirrorlist'
        echo
    done <<< "$native_repositories"
} > /tmp/tideforge-pacman.conf

# Install with a full upgrade, never -Sy followed by -S. The base image's
# packages lag the mirror, and syncing the databases without upgrading is an
# Arch partial upgrade: the solver pulls today's dependencies of $package
# against yesterday's libraries. Run 37821959508 (roost 0.1.0-3) installed
# gtk4 4.24.1 next to the image's glib2 2.88.3, and the smoke test died with
# "libgtk-4.so.1: undefined symbol: g_timeout_source_new_ns", a failure in
# the harness, not the package. The build container already runs -Syu
# (run-package-factory-cell.sh); the install must match it.
pacman --config /tmp/tideforge-pacman.conf -Syu --noconfirm "$package"

# Prove the install really happened before anything downstream trusts it. The
# defect above is exactly the kind this line exists to catch.
pacman -Q "$package"

exec bash /scripts/assert-arch-runtime-closure.sh "$package" "$@"
