"""cpptrace-devel must declare libunwind as a runtime dep on Arch.

`-DCPPTRACE_UNWIND_WITH_LIBUNWIND=ON` puts `-lunwind` on the link line, so
the shipped library carries `libunwind.so.8` in its NEEDED list. Every other
format derives that automatically -- rpmbuild generates Requires from ELF
sonames, dpkg-shlibdeps fills ${shlibs:Depends} -- but makepkg only WARNS, so
the Arch package installed cleanly and shipped a library nothing could load:

    assert-arch-runtime-closure: unresolved libraries in /usr/lib/libcpptrace.so.1
        libunwind.so.8 => not found
     -> cpptrace-devel's recipe under-declares its runtime depends.

(run 32566033598, tideforge-cpptrace-devel-arch-x86_64.) Note the smoke test
itself PASSED there -- ldconfig resolved libcpptrace.so.1 fine. It was the
runtime-closure gate that caught this, which is the whole reason that gate
compares against the declared graph rather than trusting the build.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tideforge  # noqa: E402

RECIPE = ROOT / "packages" / "cpptrace-devel" / "package.yaml"


def recipe():
    return yaml.safe_load(RECIPE.read_text(encoding="utf-8"))


def test_arch_declares_libunwind_at_runtime():
    assert "libunwind" in tideforge.target_runtime_dependencies(recipe(), "arch")


def test_the_build_option_that_creates_the_dependency_is_still_set():
    """If -DCPPTRACE_UNWIND_WITH_LIBUNWIND is ever turned off, the runtime dep
    becomes wrong rather than merely unnecessary. Pin them together."""
    assert "-DCPPTRACE_UNWIND_WITH_LIBUNWIND=ON" in recipe()["build"]["cmake_options"]


def test_the_runtime_name_is_known_good_on_arch():
    """It must also appear in the arch BUILD list, which has already resolved
    against pacman -- a runtime-only name would be unverified."""
    r = recipe()
    assert "libunwind" in tideforge.target_dependencies(r, "arch")


def test_no_other_target_declares_it():
    """rpmbuild and dpkg-shlibdeps derive this automatically. Declaring it by
    hand elsewhere would pin a package name each distro spells differently
    (libunwind-devel / libunwind-dev / libunwind8) for no benefit."""
    r = recipe()
    for target in ("el10", "ubuntu", "debian", "opensuse-tumbleweed"):
        assert tideforge.target_runtime_dependencies(r, target) == [], target


def test_the_arch_package_ships_the_library_it_needs_resolved():
    """The closure gate only fires on ELF objects the package actually
    installs. If arch ever stopped shipping the .so this test should fail
    loudly rather than the dependency silently becoming dead weight."""
    r = recipe()
    assert tideforge.ships_a_shared_library(list(r["outputs"]["rpm"]["files"]))
