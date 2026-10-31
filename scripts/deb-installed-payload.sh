#!/usr/bin/env bash
# Compare every installed payload member to the served signed Debian archive.
# Files, symlinks, directories and regular-file modes must be unchanged.
set -euo pipefail

archive=${1:?archive required}
installed_root=${2:?installed root required}
receipt=${3:?receipt required}
[ -f "$archive" ] && [ -d "$installed_root" ] || exit 1
mkdir -p "$(dirname -- "$receipt")"
rm -f -- "$receipt"

scratch=$(mktemp -d)
output_tmp=$(mktemp "${receipt}.tmp.XXXXXX")
trap 'rm -rf -- "$scratch"; rm -f -- "$output_tmp"' EXIT

dpkg-deb --extract "$archive" "$scratch"
printf 'archive\t%s\n' "$(sha256sum "$archive" | awk '{print $1}')" > "$output_tmp"
count=0
while IFS= read -r -d '' expected; do
    member=${expected#"$scratch/"}
    case "$member" in
        *$'\n'*|*$'\t'*|/*|../*|*/../*)
            echo "unsafe payload member: $member" >&2
            exit 1
            ;;
    esac
    actual="$installed_root/$member"
    if [ -L "$expected" ]; then
        [ -L "$actual" ] || { echo "missing installed symlink: $member" >&2; exit 1; }
        wanted=$(readlink -- "$expected")
        [ "$wanted" = "$(readlink -- "$actual")" ] || {
            echo "changed installed symlink: $member" >&2
            exit 1
        }
        case "$wanted" in *$'\n'*|*$'\t'*) exit 1 ;; esac
        printf 'symlink\t%s\t%s\n' "$member" "$wanted" >> "$output_tmp"
    elif [ -f "$expected" ]; then
        if [ ! -f "$actual" ] || [ -L "$actual" ]; then
            echo "missing installed file: $member" >&2
            exit 1
        fi
        wanted=$(sha256sum "$expected" | awk '{print $1}')
        observed=$(sha256sum "$actual" | awk '{print $1}')
        [ "$wanted" = "$observed" ] || {
            echo "changed installed bytes: $member" >&2
            exit 1
        }
        mode=$(stat -c '%a' "$expected")
        [ "$mode" = "$(stat -c '%a' "$actual")" ] || {
            echo "changed installed mode: $member" >&2
            exit 1
        }
        printf 'file\t%s\t%s\t%s\n' "$member" "$mode" "$wanted" >> "$output_tmp"
    elif [ -d "$expected" ]; then
        if [ ! -d "$actual" ] || [ -L "$actual" ]; then
            echo "missing installed directory: $member" >&2
            exit 1
        fi
    else
        echo "unsupported package payload type: $member" >&2
        exit 1
    fi
    count=$((count + 1))
done < <(find "$scratch" -mindepth 1 -print0)

[ "$count" -gt 0 ] || { echo 'empty package payload' >&2; exit 1; }
mv -- "$output_tmp" "$receipt"
