#!/usr/bin/env bash
# Verify only against the reviewed public.gpg, not the publisher's secret key
# or any key already imported into the runner's global GPG/RPM databases.
set -euo pipefail
export LC_ALL=C
repo=${1:?repository directory required}
arch=${2:?native architecture required}
[[ "$arch" = x86_64 || "$arch" = aarch64 ]]
trust=$(mktemp -d)
trap 'rm -rf "$trust"' EXIT
mkdir -m 0700 "$trust/gpg" "$trust/rpm"
gpg --batch --homedir "$trust/gpg" --import public.gpg
rpm --dbpath "$trust/rpm" --initdb
rpm --dbpath "$trust/rpm" --import public.gpg
test -s "$repo/repodata/repomd.xml"
test -s "$repo/repodata/repomd.xml.asc"
gpg --batch --homedir "$trust/gpg" --verify \
  "$repo/repodata/repomd.xml.asc" "$repo/repodata/repomd.xml"
count=0
while IFS= read -r -d '' package; do
  signature=$(rpm --dbpath "$trust/rpm" --checksig "$package")
  # A zero rpm exit can mean only digests checked on an unsigned package.
  grep -q 'signatures OK$' <<< "$signature"
  package_arch=$(rpm -qp --nosignature --qf '%{ARCH}' "$package")
  [[ "$package_arch" = "$arch" || "$package_arch" = noarch ]]
  count=$((count + 1))
done < <(find "$repo" -name '*.rpm' ! -name '*.src.rpm' -print0)
test "$count" -gt 0
printf 'Verified %s RPMs and repomd against committed public.gpg\n' "$count"
