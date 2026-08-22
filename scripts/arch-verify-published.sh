#!/usr/bin/env bash
# Install a published wave from the SERVED pacman repository on a clean image.
#
# The anti-#179 check for Arch: a repository that was never actually published
# must not sit behind a green workflow.
#
# In a file rather than inline for the reason arch-clean-install.sh documents at
# length: an inline heredoc inside `bash -lc '...'` silently swallowed every
# command after it, and the job still went green because the last command that
# executed had succeeded. No nesting, no heredoc.
set -euo pipefail

: "${URL:?URL is required}"
: "${PACKAGES:?PACKAGES is required}"
: "${REPO_NAME:?REPO_NAME is required}"

# Same mirror pin as the build container and the gate's clean-install: on
# 2026-08-18 fastly served a core.db naming a package every pool 404'd, so any
# sync that takes fastly's db resolves packages no mirror still carries. One
# mirror keeps db and pool in step.
echo 'Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch' > /etc/pacman.d/mirrorlist

# [tunaos] MUST come before [core] and [extra]. pacman resolves `-S <name>` by
# walking the sync repositories in configuration order and taking the FIRST
# that provides the name -- it does not compare versions across them. Listed
# last, every package name that also exists in an official Arch repository
# would be installed FROM Arch, and the packages this wave just published
# would never be exercised.
#
# That is not hypothetical: run 31113235209 built bazaar 0.9.1-1 and then
# reported 0.9.2-1 from `pacman -Q` -- extra's build, not ours. niri, greetd
# and dgop all exist in official repositories too, so this ordering is what
# makes the assertion below a statement about what we published.
{
    echo '[options]'
    echo 'Architecture = auto'
    echo 'SigLevel = Optional TrustAll'
    echo
    echo "[${REPO_NAME}]"
    echo 'SigLevel = Optional TrustAll'
    echo "Server = ${URL%/}"
    echo
    echo '[core]'
    echo 'Include = /etc/pacman.d/mirrorlist'
    echo
    echo '[extra]'
    echo 'Include = /etc/pacman.d/mirrorlist'
} > /tmp/tunaos-pacman.conf

pacman --config /tmp/tunaos-pacman.conf -Sy --noconfirm

names=$(echo "$PACKAGES" | tr ',' ' ')
# shellcheck disable=SC2086
pacman --config /tmp/tunaos-pacman.conf -S --noconfirm $names
# shellcheck disable=SC2086
pacman -Q $names

# Prove each installed package came from the published repository rather than
# from core/extra, which is the failure run 31113235209 hid.
for pkg in $names; do
  origin=$(pacman --config /tmp/tunaos-pacman.conf -Si "$pkg" | awk -F': ' '/^Repository/{print $2; exit}')
  if [ "$origin" != "$REPO_NAME" ]; then
    echo "ERROR: $pkg resolves to repository '$origin', not '$REPO_NAME'" >&2
    exit 1
  fi
done
echo "verified $(echo "$names" | wc -w) package(s) from $URL"
