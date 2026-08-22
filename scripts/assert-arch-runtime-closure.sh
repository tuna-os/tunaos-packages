#!/usr/bin/env bash
# Assert an installed Arch package's runtime dependency closure resolves.
#
# Usage: assert-arch-runtime-closure.sh <package> [smoke-command...]
#
# Runs inside a clean Arch container that has just installed <package> from an
# ephemeral repository, with nothing preinstalled beyond pacman's own
# resolution of the package's declared `depends`.
#
# Why this exists (#118). The Arch gate already stopped using --nodeps and a
# shared package superset, and validate-built-arch-package.py proves the built
# package carries every dependency the recipe declares. Neither catches the
# failure that actually reaches users: a recipe that *under*-declares. Remove a
# `depends` entry and the recipe and the package agree with each other, both
# missing it, and the gate stays green — pacman installs the package fine,
# `pacman -Q` prints its name, and nothing ever loads the binary.
#
# makepkg, unlike rpmbuild, does not derive shared-library dependencies
# automatically. Every Arch `depends` entry is hand-written, so under-declaring
# is the normal failure, not an exotic one.
#
# This script applies two gates:
#
#   1. (original) Walk every installed ELF object and fail on any DT_NEEDED
#      soname that ldd cannot resolve at all — the library is genuinely absent.
#
#   2. (added for #118) For every resolved library, trace its owning package
#      via pacman -Qo and require that the package is listed in the installed
#      package's Depends On.  A library that happens to be on disk because a
#      different declared dependency pulled it in transitively was green under
#      gate 1 alone; gate 2 turns that red and demands the recipe spell it out.
#
#      Two packages are always implicit:
#        glibc      — every ELF object links libc / ld-linux
#        gcc-libs   — libgcc_s, libstdc++ (compiler runtime, always present)
set -euo pipefail

package="${1:?usage: assert-arch-runtime-closure.sh <package> [smoke-command...]}"
shift

pacman -Q "$package"

# Build the set of declared owners: implicit system libraries, the package
# itself, every name in its Depends On -- and, TRANSITIVELY, everything those
# depend on in turn.
#
# The closure is the correction. Gate 2 originally compared against the direct
# Depends On only, which made it demand that a recipe name every package owning
# every library anywhere in its ELF closure. For a Qt application that is ~70
# entries (quickshell: audit, brotli, curl, glib2, icu, krb5, libglvnd,
# openssl, ...), none of which Arch convention would have you write: you declare
# qt6-base and icu arrives because qt6-base depends on it. namcap flags the
# redundant ones. So the strict form asked for a list that is unmaintainable and
# wrong by the distro's own standard.
#
# What gate 2 is actually for still holds, and still works here: a library that
# nothing in the closure owns is one pacman does not guarantee, and that is the
# "installs cleanly, never starts" failure. quickshell's real libcpptrace.so.1
# miss stays red under this version -- cpptrace appears nowhere in the closure
# of its declared depends.
#
# What is deliberately given up: a declared dependency could later drop its own
# dependency and take a transitively-relied-on library with it. That is the
# trade every distro makes by having a dependency graph at all, and gate 1
# still catches the result the moment it becomes unresolvable.
# The implicit compiler/system runtime. libgomp joins glibc and gcc-libs
# because Arch SPLIT it out of gcc-libs: it is the OpenMP half of the same GCC
# build, shipped alongside libgcc_s and libstdc++, and it is what the closure
# walk found still undeclared after everything else resolved --
#
#   /usr/lib/libgomp.so.1 is owned by libgomp, which quickshell does not declare
#
# -- for a package that otherwise reached 0 undeclared owners.
declare -A declared_owner
declared_owner[glibc]=1
declared_owner[gcc-libs]=1
declared_owner[libgomp]=1
declared_owner["$package"]=1

# Seed the walk with the implicit packages too, not just the package under
# test. They were marked as owners but never queued, so their OWN dependencies
# were never followed -- which is how a library reachable only through gcc-libs
# could still read as undeclared. Adding to this queue can only ever grow the
# accepted set, never shrink it, so it cannot turn a passing cell red.
closure_queue=("$package" glibc gcc-libs libgomp)
while [ ${#closure_queue[@]} -gt 0 ]; do
    current="${closure_queue[0]}"
    closure_queue=("${closure_queue[@]:1}")
    while IFS= read -r dep; do
        # Strip version constraints: "kcoreaddons (>= 6.0)" -> "kcoreaddons"
        dep="${dep%%[<>=]*}"
        dep="${dep## }"
        dep="${dep%% }"
        # pacman prints "Depends On : None" for a leaf package.
        [ -z "$dep" ] && continue
        [ "$dep" = "None" ] && continue
        if [ "${declared_owner[$dep]:-0}" -eq 0 ]; then
            declared_owner["$dep"]=1
            closure_queue+=("$dep")
        fi
    done < <(pacman -Qi "$current" 2>/dev/null | sed -n 's/^Depends On[[:space:]]*:[[:space:]]*//p' | tr ' ' '\n')
done

mapfile -t installed < <(pacman -Ql "$package" | awk '{print $2}')

checked=0
declare -a undeclared_libs=()
status=0
for path in "${installed[@]}"; do
    [ -f "$path" ] || continue
    # Only ELF objects have a link-time closure to resolve.
    case "$(head -c 4 "$path" 2>/dev/null || true)" in
    $'\x7f'ELF) ;;
    *) continue ;;
    esac

    checked=$((checked + 1))

    # ---- gate 1: genuinely absent libraries --------------------------------
    missing=$(ldd "$path" 2>/dev/null | grep -F 'not found' || true)
    if [ -n "$missing" ]; then
        echo "assert-arch-runtime-closure: unresolved libraries in $path" >&2
        echo "$missing" >&2
        echo "  -> ${package}'s recipe under-declares its runtime depends." >&2
        status=1
        continue
    fi

    # ---- gate 2: transitively-present but undeclared -----------------------
    # ldd lines:   libfoo.so.0 => /usr/lib/libfoo.so.0 (0x…)
    # The vDSO and ld-linux have no meaningful owning package; skip them.
    while IFS= read -r ldd_line; do
        resolved_path="${ldd_line#*=> }"
        resolved_path="${resolved_path%% (*}"
        case "$resolved_path" in
            /usr/lib/* | /lib/*) ;;
            *) continue ;;
        esac
        owning_pkg=$(pacman -Qo "$resolved_path" 2>/dev/null | sed -n 's/.* is owned by \([^ ]*\) .*/\1/p' || true)
        [ -z "$owning_pkg" ] && continue
        if [ "${declared_owner[$owning_pkg]:-0}" -eq 0 ]; then
            echo "assert-arch-runtime-closure: undeclared dependency in $path" >&2
            echo "  $ldd_line" >&2
            echo "  -> $resolved_path is owned by $owning_pkg, which ${package} does not declare." >&2
            undeclared_libs+=("$owning_pkg:$resolved_path")
            status=1
        fi
    done < <(ldd "$path" 2>/dev/null | grep -E '\s=>\s' || true)
done

echo "assert-arch-runtime-closure: checked $checked ELF object(s) in $package"

if [ "$checked" -eq 0 ]; then
    echo "assert-arch-runtime-closure: no ELF objects — closure not applicable to $package"
fi

if [ ${#undeclared_libs[@]} -gt 0 ]; then
    {
        echo "assert-arch-runtime-closure: undeclared runtime dependencies:"
        printf '%s\n' "${undeclared_libs[@]}" | sort -u | while IFS=: read -r pkg lib; do
            echo "  $pkg  (provides $lib)"
        done
        echo "  -> ${package}'s recipe under-declares its runtime depends."
    } >&2
fi

if [ "$#" -gt 0 ]; then
    echo "assert-arch-runtime-closure: smoke: $*"
    "$@"
fi

exit "$status"
