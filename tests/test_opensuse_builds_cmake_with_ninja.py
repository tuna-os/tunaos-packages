"""openSUSE's %cmake_build must drive the generator the recipe asked for.

quickshell's two Tumbleweed cells were the last red pair after every other
cause had been fixed, and the reason was not a dependency: openSUSE's CMake
macros derive the generator FROM %__builder rather than from anything the
spec passes.

Verified against the packaging sources, not inferred (openSUSE `rpm/cmake`,
cmake.macros):

    %__builder %__make
    %cmake ... %if "%__builder" == "%__make"  -G"Unix Makefiles"
                %else                          -GNinja
    %cmake_build %__builder \\%__builder_verbose %{?_smp_mflags}
    %cmake_install DESTDIR=%{buildroot} %__builder install -C %__builddir

So `%cmake -G Ninja` produced a Ninja tree -- our -G is passed last and wins
-- and `%cmake_build` then ran make against it:

    make: *** No targets specified and no makefile found.  Stop.

Setting %__builder repairs the whole chain at once: %cmake emits -GNinja
itself, %__builder_verbose becomes -v, %cmake_build runs `ninja -v`, and
%cmake_install runs `ninja install -C build`.

Fedora and EL never read %__builder -- zero references in cmake's
macros.cmake.in and in redhat-rpm-config/macros, both checked against
rawhide -- so the line is inert there. That is why it is emitted for every
RPM target instead of being gated on a target name: a name list silently
regresses when the next openSUSE-family target is added.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tideforge  # noqa: E402

BUILDER = "%global __builder %__ninja"


def recipe(**build):
    return {
        "schema": 1,
        "name": "demo",
        "version": "1.0",
        "release": 1,
        "summary": "s",
        "description": "d",
        "license": "MIT",
        "source": {"url": "https://example.invalid/demo-1.0.tar.gz", "sha256": "0" * 64},
        "build_system": "cmake",
        "build": build,
        "targets": ["el10", "opensuse-tumbleweed"],
        "files": {"common": ["/usr/bin/demo"]},
    }


def spec_for(r, target="opensuse-tumbleweed") -> str:
    rendered = tideforge.render_rpm(r, target)
    return rendered[next(k for k in rendered if k.endswith(".spec"))]


def test_a_ninja_recipe_sets_the_builder():
    assert BUILDER in spec_for(recipe(cmake_generator="Ninja"))


def test_a_default_generator_recipe_does_not():
    """Only a recipe that asked for Ninja may redirect the builder; leaving
    it set for a Makefiles build would run ninja against a Makefile tree --
    the same failure with the operands swapped."""
    assert BUILDER not in spec_for(recipe())


def test_a_non_cmake_recipe_does_not():
    r = recipe()
    r["build_system"] = "meson"
    r["build"] = {}
    assert BUILDER not in spec_for(r)


def test_the_builder_precedes_the_cmake_invocation():
    """%__builder is read while %cmake and %cmake_build expand, so a define
    placed after them would parse but change nothing."""
    text = spec_for(recipe(cmake_generator="Ninja"))
    assert text.index(BUILDER) < text.index("%cmake ")
    assert text.index(BUILDER) < text.index("%cmake_build")


def test_it_is_emitted_on_el10_too_so_the_rule_is_mechanism_not_target_name():
    """Inert on EL (nothing there reads %__builder). Asserted so that a later
    change cannot quietly narrow this to a hard-coded openSUSE check."""
    assert BUILDER in spec_for(recipe(cmake_generator="Ninja"), "el10")


def test_the_builder_line_does_not_displace_the_debug_package_global():
    """Both land in the same preamble slot; a recipe needing both must get
    both, in a spec that still parses."""
    r = recipe(cmake_generator="Ninja", debug_package=False)
    text = spec_for(r)
    assert "%global debug_package %{nil}" in text
    assert BUILDER in text
    assert text.startswith("%global debug_package %{nil}\n" + BUILDER)


def test_quickshell_the_recipe_that_hit_this_gets_it():
    r = yaml.safe_load((ROOT / "packages" / "quickshell" / "package.yaml").read_text(encoding="utf-8"))
    assert BUILDER in spec_for(r)


def test_quickshell_opensuse_gets_a_ninja_to_run():
    """The builder redirect is useless without the binary. openSUSE spells it
    `ninja` (its rpm/ninja package is Name: ninja); EL and Debian spell it
    ninja-build."""
    r = yaml.safe_load((ROOT / "packages" / "quickshell" / "package.yaml").read_text(encoding="utf-8"))
    assert "ninja" in tideforge.target_dependencies(r, "opensuse-tumbleweed")
    assert "BuildRequires: ninja\n" in spec_for(r)


def test_an_implied_capability_does_not_double_up_a_hand_listed_name():
    """Deriving ninja means a recipe that still names it explicitly would get
    it twice. Harmless to every package manager here, but a duplicated
    BuildRequires/Build-Depends reads as a mistake to whoever audits the
    rendered output, and quickshell's own lists named it until this change.
    """
    r = recipe(cmake_generator="Ninja")
    r["dependencies"] = {"build": {"targets": {"el10": ["ninja-build", "gcc"]}}}
    resolved = tideforge.target_dependencies(r, "el10")
    assert resolved.count("ninja-build") == 1, resolved
    assert "gcc" in resolved


def test_deduplication_keeps_the_first_occurrence_and_the_rest_of_the_order():
    assert tideforge.deduplicate(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
