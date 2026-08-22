#!/usr/bin/env bats
# scripts/assert-arch-runtime-closure.sh — gate 2's declared-owner set.
#
# Gate 2 exists because makepkg, unlike rpmbuild, derives nothing: every Arch
# `depends` entry is hand-written, so under-declaring is the normal failure and
# it is invisible — pacman installs the package, `pacman -Q` prints its name,
# and nothing ever loads the binary.
#
# It originally compared each library's owner against the package's DIRECT
# Depends On. That demanded a recipe name every package owning every library
# anywhere in its ELF closure — ~70 entries for a Qt application, which Arch
# convention does not write (you declare qt6-base and icu comes with it, and
# namcap flags the redundant ones).
#
# It now compares against the TRANSITIVE closure of the declared depends. These
# tests pin both halves: the closure is accepted, and a library owned by
# nothing in it is still red.
#
# The real script runs against a mocked pacman, so it measures behaviour rather
# than source text.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/assert-arch-runtime-closure.sh"

setup() {
	BIN="${BATS_TEST_TMPDIR}/bin"
	mkdir -p "$BIN"
	LIBDIR="${BATS_TEST_TMPDIR}/usr/lib"
	mkdir -p "$LIBDIR"

	# A REAL ELF magic header, not an empty file. The script selects objects
	# with `head -c 4` and skips anything that is not \x7fELF, so an empty
	# payload makes it check nothing and exit 0 -- every test here passed
	# vacuously, including the one asserting a failure, until this was fixed.
	printf '\177ELF\002\001\001\000' > "${LIBDIR}/quickshell"

	# depends graph: quickshell -> qt6-base -> icu ; cpptrace is NOT in it.
	cat > "${BIN}/pacman" <<STUB
#!/usr/bin/env bash
case "\$1" in
  -Q)  echo "\$2 1.0-1" ;;
  -Qi)
    case "\$2" in
      quickshell) echo "Depends On     : qt6-base" ;;
      qt6-base)   echo "Depends On     : icu" ;;
      icu)        echo "Depends On     : None" ;;
      *)          echo "Depends On     : None" ;;
    esac
    ;;
  -Ql) echo "\$2 ${LIBDIR}/quickshell" ;;
  -Qo)
    case "\$2" in
      *libicuuc*)     echo "\$2 is owned by icu 78-1" ;;
      *libcpptrace*)  echo "\$2 is owned by cpptrace 1.0.4-1" ;;
      *)              echo "\$2 is owned by glibc 2.43-1" ;;
    esac
    ;;
esac
STUB
	chmod +x "${BIN}/pacman"

}

# Drives the mocked ldd. The resolved paths must be under /usr/lib: gate 2
# skips anything outside /usr/lib and /lib, so sandbox paths would make it
# check nothing and pass. The script never stats these -- it only feeds them to
# `pacman -Qo` -- so they do not need to exist.
write_ldd() {
	cat > "${BIN}/ldd" <<STUB
#!/usr/bin/env bash
for lib in ${1}; do
  echo "	\$lib => /usr/lib/\$lib (0x00007f0000000000)"
done
STUB
	chmod +x "${BIN}/ldd"
}

run_gate() {
	PATH="${BIN}:$PATH" run bash "$SCRIPT" quickshell
}

@test "a library owned by a TRANSITIVE dependency is accepted" {
	# icu is not in quickshell's Depends On; it is reached through qt6-base.
	# Under the old direct-only comparison this was the failure that demanded
	# ~70 hand-written entries.
	write_ldd "libicuuc.so.78"
	run_gate
	[ "$status" -eq 0 ]
}

@test "a library owned by nothing in the closure is still red" {
	# The regression gate 2 exists for, and quickshell's real one: cpptrace
	# appears nowhere in the declared closure, so libcpptrace.so.1 must fail.
	write_ldd "libcpptrace.so.1"
	run_gate
	[ "$status" -ne 0 ]
	[[ "$output" == *"cpptrace"* ]]
	[[ "$output" == *"under-declares"* ]]
}

@test "glibc stays implicit" {
	write_ldd "libc.so.6"
	run_gate
	[ "$status" -eq 0 ]
}

@test "the closure walk terminates on a dependency cycle" {
	# pacman graphs are acyclic in practice, but a BFS with no visited-set
	# would hang forever on a malformed one and take the whole cell with it.
	cat > "${BIN}/pacman" <<STUB
#!/usr/bin/env bash
case "\$1" in
  -Q)  echo "\$2 1.0-1" ;;
  -Qi)
    case "\$2" in
      quickshell) echo "Depends On     : a" ;;
      a)          echo "Depends On     : b" ;;
      b)          echo "Depends On     : a quickshell" ;;
      *)          echo "Depends On     : None" ;;
    esac
    ;;
  -Ql) echo "\$2 ${LIBDIR}/quickshell" ;;
  -Qo) echo "\$2 is owned by b 1.0-1" ;;
esac
STUB
	chmod +x "${BIN}/pacman"
	write_ldd "libb.so.1"
	run_gate
	[ "$status" -eq 0 ]
}

@test "libgomp is implicit — Arch split it out of gcc-libs" {
	# The OpenMP half of the same GCC build that ships libgcc_s and libstdc++.
	# It was the LAST undeclared owner on quickshell after the closure walk
	# resolved the other 68, and declaring it in a recipe would be as odd as
	# declaring glibc.
	write_ldd "libgomp.so.1"
	cat > "${BIN}/pacman" <<STUB
#!/usr/bin/env bash
case "\$1" in
  -Q)  echo "\$2 1.0-1" ;;
  -Qi) echo "Depends On     : None" ;;
  -Ql) echo "\$2 ${LIBDIR}/quickshell" ;;
  -Qo) echo "\$2 is owned by libgomp 15.2.0-1" ;;
esac
STUB
	chmod +x "${BIN}/pacman"
	run_gate
	[ "$status" -eq 0 ]
}

@test "the implicit packages' OWN dependencies are walked too" {
	# They were marked as owners but never queued, so a library reachable only
	# through gcc-libs still read as undeclared. Seeding the walk with them
	# fixes that, and can only ever grow the accepted set.
	cat > "${BIN}/pacman" <<STUB
#!/usr/bin/env bash
case "\$1" in
  -Q)  echo "\$2 1.0-1" ;;
  -Qi)
    case "\$2" in
      quickshell) echo "Depends On     : None" ;;
      gcc-libs)   echo "Depends On     : some-gcc-runtime" ;;
      *)          echo "Depends On     : None" ;;
    esac
    ;;
  -Ql) echo "\$2 ${LIBDIR}/quickshell" ;;
  -Qo) echo "\$2 is owned by some-gcc-runtime 1.0-1" ;;
esac
STUB
	chmod +x "${BIN}/pacman"
	write_ldd "libsomething.so.1"
	run_gate
	[ "$status" -eq 0 ]
}
