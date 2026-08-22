"""A recipe that asks for Ninja must be BUILT with ninja on deb, not just
configured with it.

debhelper's `cmake` buildsystem runs `make` in dh_auto_build whatever the
generator is. tideforge passed `-G Ninja` through dh_auto_configure, so CMake
wrote build.ninja and dh_auto_build then ran make against it -- on every deb
cell of every recipe that asked for Ninja (run 32556308211):

    cd obj-x86_64-linux-gnu && make -j4 INSTALL=... VERBOSE=1
    make[1]: *** No targets specified and no makefile found.  Stop.
    dh_auto_build: error: ... returned exit code 2

The configure line carried debhelper's own `-GUnix Makefiles` AND our
`-G Ninja`; CMake took the last one, which is why it configured fine and only
came apart at build time.

`cmake+ninja` is debhelper's generator-aware variant: it passes -GNinja at
configure time and builds with ninja. The generator must then not be passed by
hand too -- that is what produced two -G flags on one command line.

The RPM path is deliberately untouched: Fedora/EL's %cmake_build calls
`cmake --build`, which reads the generator out of the cache and does the right
thing already. (openSUSE's %cmake_build does NOT -- that is the same class of
bug on a different format, tracked separately.)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tideforge  # noqa: E402


# Derived from a real cmake recipe rather than hand-built, so the fixture
# cannot drift out of schema as the renderer gains required keys.
BASE = yaml.safe_load(
    (ROOT / "packages" / "quickshell" / "package.yaml").read_text(encoding="utf-8")
)


def recipe(**build) -> dict:
    import copy
    r = copy.deepcopy(BASE)
    r["build"] = build
    return r


def rules(r: dict) -> str:
    return tideforge.render_deb(r, "ubuntu")["debian/rules"]


def test_a_ninja_recipe_uses_the_ninja_buildsystem():
    assert "--buildsystem=cmake+ninja" in rules(recipe(cmake_generator="Ninja"))


def test_the_generator_is_not_also_passed_by_hand():
    """Two -G flags on one cmake line is the ambiguity that caused this."""
    text = rules(recipe(cmake_generator="Ninja", cmake_options=["-DFOO=ON"]))
    assert "-G Ninja" not in text, text
    assert "-DFOO=ON" in text, text


def test_the_other_cmake_options_still_reach_configure():
    """Dropping the generator must not drop the rest of the options with it."""
    text = rules(recipe(cmake_generator="Ninja", cmake_options=["-DA=1", "-DB=2"]))
    assert "dh_auto_configure -- -DA=1 -DB=2" in text, text


def test_a_recipe_without_the_generator_is_untouched():
    """The plain cmake buildsystem stays the default; this fix applies only to
    recipes that actually asked for Ninja."""
    text = rules(recipe(cmake_options=["-DA=1"]))
    assert "--buildsystem=cmake\n" in text, text
    assert "cmake+ninja" not in text, text


def test_the_rpm_path_is_not_changed_by_this():
    """Fedora/EL's %cmake_build calls `cmake --build`, which reads the
    generator from the cache. The RPM side must keep passing -G Ninja to
    %cmake, so this test fails if the deb fix is ever "tidied" into the shared
    generator helper."""
    r = recipe(cmake_generator="Ninja")
    build, _install = tideforge.rpm_build_lines(r["build_system"], r)
    assert "-G Ninja" in build, build


def test_every_ninja_recipe_in_the_repo_gets_a_ninja_on_every_target_it_declares():
    """cmake+ninja invokes ninja, so the buildroot must actually have it.

    Widened from deb to EVERY declared target. Restricting it to ubuntu and
    debian is what let openSUSE ship a Ninja recipe with no ninja installed
    (#478): its %cmake_build then drove a Ninja tree with make. A recipe
    adding cmake_generator now gets ninja on every target automatically, and
    this asserts the resolution actually produced one rather than trusting
    that it did.
    """
    for path in sorted((ROOT / "packages").glob("*/package.yaml")):
        r = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not (r.get("build") or {}).get("cmake_generator"):
            continue
        for target in r.get("targets") or []:
            resolved = tideforge.target_dependencies(r, target)
            assert any(d in ("ninja", "ninja-build") for d in resolved), (path.parent.name, target, resolved)
