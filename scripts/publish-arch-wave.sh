#!/usr/bin/env bash
# Merge a wave of Arch packages into a pacman repository and index it.
#
# The pacman counterpart of scripts/publish-rpm-wave.sh. Every rule below is
# either inherited from that script's incident history or specific to how
# pacman resolves a repository over HTTP.
set -euo pipefail

# Runs INSIDE the Arch container, not on the runner host. repo-add ships in
# Arch's `pacman` package and does not exist on ubuntu-latest, so a host-side
# invocation would fail on the first dispatch -- caught before that happened,
# but only just. gnupg is in the same base image, so signing happens here too
# rather than mounting the runner's ~/.gnupg (which brings permission and
# uid-mapping problems of its own).
staged="" repo="" name="" key=""
while [ $# -gt 0 ]; do
  case "$1" in
    --staged) staged="$2"; shift 2 ;;
    --repo)   repo="$2";   shift 2 ;;
    --name)   name="$2";   shift 2 ;;
    --key)    key="$2";    shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
: "${staged:?--staged is required}"
: "${repo:?--repo is required}"
: "${name:?--name is required}"

command -v repo-add >/dev/null || {
  echo "ERROR: repo-add not found. This script must run inside an Arch container;" >&2
  echo "       repo-add ships in pacman and is not available on the runner host." >&2
  exit 1
}

# Import the signing key here rather than relying on an inherited keyring.
if [ -z "$key" ] && [ -n "${GPG_PRIVATE_KEY:-}" ]; then
  printf '%s' "$GPG_PRIVATE_KEY" | gpg --batch --import
  key=$(gpg --list-secret-keys --with-colons | awk -F: '/^sec:/{print $5; exit}')
  [ -n "$key" ] || { echo "ERROR: imported a GPG key but could not read its id" >&2; exit 1; }
fi

shopt -s nullglob
incoming=("$staged"/*.pkg.tar.*)
# Refuse an empty wave. rclone sync makes the destination match the source, so
# publishing "nothing" would DELETE the served repository rather than leave it
# alone -- the #124 / INCIDENT-repo-wipe-gnome shape.
if [ ${#incoming[@]} -eq 0 ]; then
  echo "ERROR: no packages staged; refusing to publish an empty wave" >&2
  exit 1
fi

before=("$repo"/*.pkg.tar.*)
mkdir -p "$repo"
cp -f "${incoming[@]}" "$repo"/
after=("$repo"/*.pkg.tar.*)

# Never let the tree shrink. A wave adds or replaces; a smaller result means
# something was dropped on the floor.
if [ ${#after[@]} -lt ${#before[@]} ]; then
  echo "ERROR: repository shrank from ${#before[@]} to ${#after[@]} packages; refusing to sync" >&2
  exit 1
fi

# Detached signatures per package. The gate's arch-clean-install.sh notes that
# CI artifacts are deliberately unsigned and that production promotion signs
# separately -- this is that step. Without .sig files a consumer configured
# with SigLevel = Required cannot install anything from here.
if [ -n "$key" ]; then
  for pkg in "$repo"/*.pkg.tar.*; do
    case "$pkg" in *.sig) continue ;; esac
    rm -f "$pkg.sig"
    gpg --batch --yes --pinentry-mode loopback ${GPG_PASSPHRASE:+--passphrase "$GPG_PASSPHRASE"} \
      --detach-sign --no-armor --local-user "$key" "$pkg"
  done
fi

rm -f "$repo/$name".db* "$repo/$name".files*
sign_args=()
[ -n "$key" ] && sign_args+=(--sign --key "$key")
repo-add "${sign_args[@]+"${sign_args[@]}"}" "$repo/$name.db.tar.gz" "$repo"/*.pkg.tar.zst

# repo-add writes <name>.db.tar.gz, but pacman requests <name>.db. On a local
# filesystem that is a symlink repo-add creates; over HTTP from an object
# store there are no symlinks, so the db must exist under the requested name
# as a real object. arch-clean-install.sh hit exactly this and copies the file
# for the same reason. Same for .files, which pacman -F requests.
cp -f "$repo/$name.db.tar.gz" "$repo/$name.db"
[ -f "$repo/$name.files.tar.gz" ] && cp -f "$repo/$name.files.tar.gz" "$repo/$name.files"
if [ -n "$key" ]; then
  [ -f "$repo/$name.db.tar.gz.sig" ] && cp -f "$repo/$name.db.tar.gz.sig" "$repo/$name.db.sig"
  [ -f "$repo/$name.files.tar.gz.sig" ] && cp -f "$repo/$name.files.tar.gz.sig" "$repo/$name.files.sig"
fi

echo "published ${#after[@]} package(s) into $name"
