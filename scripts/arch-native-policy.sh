#!/usr/bin/env bash
# Configure only native official repositories; never weaken package signatures.
# configure [expected-architecture] | repositories
set -euo pipefail

actual=$(uname -m)
case "$actual" in
  x86_64) keyring=archlinux; mirror='https://geo.mirror.pkgbuild.com/$repo/os/$arch' ;;
  aarch64) keyring=archlinuxarm; mirror='https://fl.us.mirror.archlinuxarm.org/$arch/$repo' ;;
  *) echo 'unsupported native Arch architecture' >&2; exit 2 ;;
esac
expected=${2:-$actual}
[[ "$actual" == "$expected" ]] || { echo 'native Arch architecture differs from requested build' >&2; exit 2; }
[[ $(pacman-conf Architecture) == "$actual" ]] || { echo 'pacman native architecture differs from kernel architecture' >&2; exit 2; }

repo_list=$(pacman-conf --repo-list)
mapfile -t repositories <<< "$repo_list"
(( ${#repositories[@]} > 0 )) || { echo 'native repository list missing' >&2; exit 2; }
declare -A seen_repositories=()
for repo in "${repositories[@]}"; do
  [[ -z ${seen_repositories[$repo]:-} ]] || { echo 'duplicate native repository' >&2; exit 2; }
  seen_repositories[$repo]=1
  case "$actual:$repo" in
    x86_64:core|x86_64:extra|x86_64:multilib|aarch64:core|aarch64:extra|aarch64:alarm|aarch64:aur) ;;
    *) echo 'unexpected native Arch repository' >&2; exit 2 ;;
  esac
done
[[ -n ${seen_repositories[core]:-} && -n ${seen_repositories[extra]:-} ]] || {
  echo 'native core and extra repositories are required' >&2; exit 2;
}
operation=${1:?configure or repositories required}
[[ "$operation" == configure || "$operation" == repositories ]] || { echo 'unsupported native policy operation' >&2; exit 2; }

# pacman-conf emits the authored per-repository override; an empty result
# inherits global SigLevel. Apply tokens in order, including scoped tokens.
global=$(pacman-conf SigLevel)
for repo in "${repositories[@]}"; do
  override=$(pacman-conf --repo "$repo" SigLevel)
  requirement=Required
  trust=TrustedOnly
  for token in $global $override; do
    case "$token" in
      Required|PackageRequired) requirement=Required ;;
      Optional|PackageOptional) requirement=Optional ;;
      Never|PackageNever) requirement=Never ;;
      TrustedOnly|PackageTrustedOnly) trust=TrustedOnly ;;
      TrustAll|PackageTrustAll) trust=TrustAll ;;
      DatabaseRequired|DatabaseOptional|DatabaseNever|DatabaseTrustedOnly|DatabaseTrustAll) ;;
      *) echo 'unknown native signature policy token' >&2; exit 2 ;;
    esac
  done
  [[ "$requirement:$trust" == Required:TrustedOnly ]] || { echo 'native packages require trusted signatures' >&2; exit 2; }
done
[[ "$operation" == repositories ]] && { printf '%s\n' "${repositories[@]}"; exit 0; }
pacman-key --init
pacman-key --populate "$keyring"
printf 'Server = %s\n' "$mirror" > /etc/pacman.d/mirrorlist
