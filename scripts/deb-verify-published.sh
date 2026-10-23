#!/usr/bin/env bash
# Install a deb wave from its signed flat repository and collect exact evidence.
# The receipt consumer binds this evidence to the gated ActionResult.
set -euo pipefail

packages=${PACKAGES:?PACKAGES is required}
url=${URL:?URL is required}
evidence=${EVIDENCE_DIR:?EVIDENCE_DIR is required}
keyring=${KEYRING:-/keys/tunaos-public.gpg}

case "$url" in
    https://repo.tunaos.org/tideforge/*/) ;;
    *) echo "unexpected served repository: $url" >&2; exit 1 ;;
esac
[ -s "$keyring" ] || { echo "missing pinned repository key: $keyring" >&2; exit 1; }
mkdir -p "$evidence"
rm -rf -- "${evidence:?}"/*

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl gpg gzip python3 libglib2.0-bin

printf 'deb [signed-by=%s] %s ./' "$keyring" "${url%/}" \
    > /etc/apt/sources.list.d/tideforge.list
printf 'Package: *\nPin: origin ""\nPin-Priority: 1001\n' \
    > /etc/apt/preferences.d/tideforge.pref
apt-get update > /tmp/tideforge-apt-update.log 2>&1 || {
    cat /tmp/tideforge-apt-update.log >&2
    exit 1
}
cat /tmp/tideforge-apt-update.log
if grep -q 'Failed to fetch' /tmp/tideforge-apt-update.log; then
    echo 'declared served index could not be fetched' >&2
    exit 1
fi

read -r -a package_names <<< "$packages"
[ "${#package_names[@]}" -gt 0 ] || { echo 'empty package list' >&2; exit 1; }
for package in "${package_names[@]}"; do
    [[ "$package" =~ ^[A-Za-z0-9][A-Za-z0-9+.-]*$ ]] || {
        echo "unsafe package name: $package" >&2
        exit 1
    }
done
apt-get install -y --no-install-recommends "${package_names[@]}"

curl --fail --location --silent --show-error --retry 3 \
    "${url%/}/InRelease" -o "$evidence/InRelease"
gpg --batch --no-options --no-default-keyring --keyring "$keyring" \
    --status-fd 1 --verify "$evidence/InRelease" \
    > "$evidence/signature-status.txt" 2> "$evidence/signature.stderr"
curl --fail --location --silent --show-error --retry 3 \
    "${url%/}/Packages.gz" -o "$evidence/Packages.gz"
gzip -dc "$evidence/Packages.gz" > "$evidence/Packages"

for package in "${package_names[@]}"; do
    observed="$evidence/$package"
    mkdir -p "$observed"
    version=$(dpkg-query -W -f='${Version}' "$package")
    installed_arch=$(dpkg-query -W -f='${Architecture}' "$package")
    fields=$(python3 - "$evidence/Packages" "$package" "$version" "$installed_arch" <<'PY'
import sys
from pathlib import Path

path, wanted_package, wanted_version, wanted_arch = sys.argv[1:]
rows = []
for raw in Path(path).read_text(encoding="utf-8").split("\n\n"):
    fields = {}
    for line in raw.splitlines():
        if line and not line[0].isspace() and ": " in line:
            key, value = line.split(": ", 1)
            fields[key] = value
    if (fields.get("Package"), fields.get("Version"), fields.get("Architecture")) == (
        wanted_package,
        wanted_version,
        wanted_arch,
    ):
        rows.append(fields)
if len(rows) != 1:
    raise SystemExit(
        f"expected one served Packages entry for {wanted_package} {wanted_version} {wanted_arch}, found {len(rows)}"
    )
entry = rows[0]
for key in ("Filename", "SHA256", "Size"):
    if not entry.get(key):
        raise SystemExit(f"served Packages entry lacks {key}")
print("\t".join(entry[key] for key in ("Filename", "SHA256", "Size")))
PY
)
IFS=$'\t' read -r filename expected_sha expected_size <<< "$fields"
case "$filename" in
    /*|../*|*/../*|*//*|*$'\n'*|*$'\t'*)
        echo "unsafe served filename: $filename" >&2
        exit 1
        ;;
esac
archive_name=${filename##*/}
[[ "$archive_name" =~ ^[A-Za-z0-9][A-Za-z0-9+_.~-]*\.deb$ ]] || {
    echo "unsafe served archive basename: $archive_name" >&2
    exit 1
}
[[ "$expected_sha" =~ ^[0-9a-f]{64}$ ]] || { echo 'invalid served SHA256' >&2; exit 1; }
[[ "$expected_size" =~ ^[0-9]+$ ]] || { echo 'invalid served size' >&2; exit 1; }
archive="$observed/$archive_name"
curl --fail --location --silent --show-error --retry 3 \
    "${url%/}/$filename" -o "$archive"
actual_sha=$(sha256sum "$archive" | awk '{print $1}')
actual_size=$(stat -c '%s' "$archive")
[ "$actual_sha" = "$expected_sha" ] || { echo "served bytes changed: $package" >&2; exit 1; }
[ "$actual_size" = "$expected_size" ] || { echo "served size changed: $package" >&2; exit 1; }

printf '%s\n' "Package: $package" "Version: $version" \
    "Architecture: $installed_arch" "Filename: $filename" \
    "SHA256: $actual_sha" "Size: $actual_size" > "$observed/index-entry.txt"
dpkg-deb -f "$archive" Package Version Architecture Depends > "$observed/package-info.txt"
dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\n' \
    | sort > "$observed/installed-packages.tsv"
bash /scripts/deb-installed-payload.sh "$archive" / "$observed/installed-payload.tsv"
printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$package" "$version" "$installed_arch" "$filename" "$actual_sha" "$actual_size" \
    > "$observed/installed.tsv"

if [ "$package" = tuna-desktop ]; then
    gsettings list-schemas > "$observed/installed-schemas.txt"
    command -v canberra-gtk-play > "$observed/canberra-command.txt"
    for sound in \
        /usr/share/sounds/freedesktop/stereo/bell.oga \
        /usr/share/sounds/freedesktop/stereo/audio-volume-change.oga; do
        test -s "$sound"
        printf '%s\n' "$sound" >> "$observed/sound-files.txt"
    done
fi
done
