#!/usr/bin/env bash
# Compare every installed payload member to the verified signed archive.
# Metadata inside the archive is excluded; files, symlinks and modes are exact.
set -euo pipefail
archive=${1:?archive required}
installed_root=${2:?installed root required}
receipt=${3:?receipt required}
[ -f "$archive" ] && [ -d "$installed_root" ] || exit 1
rm -f -- "$receipt"
scratch=$(mktemp -d)
output_tmp=$(mktemp "${receipt}.tmp.XXXXXX")
members=$(mktemp)
trap 'rm -rf -- "$scratch"; rm -f -- "$output_tmp" "$members"' EXIT
# bsdtar rejects traversal and extraction through symlinks by default. Never
# enable -P or --insecure; the signed package still must stay in owned scratch.
bsdtar -xf "$archive" -C "$scratch"
archive_sha=$(sha256sum "$archive" | awk '{print $1}')
printf 'archive\t%s\n' "$archive_sha" > "$output_tmp"
count=0
find "$scratch" -mindepth 1 -print0 > "$members"
while IFS= read -r -d '' expected; do
    member=${expected#"$scratch/"}
    case "$member" in
        .PKGINFO|.BUILDINFO|.MTREE|.INSTALL|.CHANGELOG) continue ;;
        *$'\n'*|*$'\t'*|/*|../*|*/../*) echo 'unsafe payload member' >&2; exit 1 ;;
    esac
    actual="$installed_root/$member"
    if [ -L "$expected" ]; then
        [ -L "$actual" ] || { echo "missing installed symlink: $member" >&2; exit 1; }
        wanted=$(readlink -- "$expected")
        [ "$wanted" = "$(readlink -- "$actual")" ] || { echo "changed installed symlink: $member" >&2; exit 1; }
        case "$wanted" in *$'\n'*|*$'\t'*) exit 1 ;; esac
        printf 'symlink\t%s\t%s\n' "$member" "$wanted" >> "$output_tmp"
    elif [ -f "$expected" ]; then
        [ -f "$actual" ] && [ ! -L "$actual" ] || { echo "missing installed file: $member" >&2; exit 1; }
        wanted=$(sha256sum "$expected" | awk '{print $1}')
        observed=$(sha256sum "$actual" | awk '{print $1}')
        [ "$wanted" = "$observed" ] || { echo "changed installed bytes: $member" >&2; exit 1; }
        mode=$(stat -c '%a' "$expected")
        [ "$mode" = "$(stat -c '%a' "$actual")" ] || { echo "changed installed mode: $member" >&2; exit 1; }
        printf 'file\t%s\t%s\t%s\n' "$member" "$mode" "$wanted" >> "$output_tmp"
    elif [ -d "$expected" ]; then
        [ -d "$actual" ] && [ ! -L "$actual" ] || { echo "missing installed directory: $member" >&2; exit 1; }
        continue
    else
        echo "unsupported package payload type: $member" >&2; exit 1
    fi
    count=$((count + 1))
done < "$members"
[ "$count" -gt 0 ] || { echo 'empty package payload' >&2; exit 1; }
mv -- "$output_tmp" "$receipt"
