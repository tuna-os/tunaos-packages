"""quickshell must not ship an $ORIGIN RUNPATH in its system RPM.

`tideforge-quickshell-el10-aarch64` compiled cleanly for twelve minutes and
then failed its validation step (run 32534325163):

    quickshell.aarch64: E: binary-or-shlib-defines-rpath /usr/bin/quickshell
        (RUNPATH: $ORIGIN:$ORIGIN/../lib64)
    lint-generated-rpm: FATAL finding 'binary-or-shlib-defines-rpath'

`binary-or-shlib-defines-rpath` has been in `lint-generated-rpm.sh`'s curated
fatal set since the factory was unified (#430), so this is not a newly strict
gate -- and the finding is correct rather than a false positive. From
/usr/bin, `$ORIGIN` is /usr/bin (which holds no libraries) and
`$ORIGIN/../lib64` is /usr/lib64 (already on the default loader path), so the
RUNPATH buys nothing in an FHS install while making the package
relocation-sensitive.

The fix drops it at the source with -DCMAKE_SKIP_INSTALL_RPATH=ON rather than
exempting quickshell from the check, because exempting it would silence the
same real defect in every future recipe that trips it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tideforge  # noqa: E402

RECIPE = ROOT / "packages" / "quickshell" / "package.yaml"
LINTER = ROOT / "scripts" / "lint-generated-rpm.sh"


def recipe() -> dict:
    return yaml.safe_load(RECIPE.read_text(encoding="utf-8"))


def test_the_recipe_skips_the_install_rpath():
    assert "-DCMAKE_SKIP_INSTALL_RPATH=ON" in recipe()["build"]["cmake_options"]


def test_the_flag_survives_into_the_rendered_cmake_invocation():
    """A recipe key that the renderer drops on the floor would fix nothing."""
    assert "-DCMAKE_SKIP_INSTALL_RPATH=ON" in tideforge.cmake_options(recipe())


def test_the_jemalloc_option_is_not_lost():
    """EL10 has no native jemalloc-devel; that option must stay alongside."""
    assert "-DUSE_JEMALLOC=OFF" in tideforge.cmake_options(recipe())


def test_the_rpath_check_is_still_fatal():
    """The recipe fix only matters while the gate that caught it stays armed.

    If someone later drops the check from the curated set, this recipe change
    becomes invisible cargo -- and the next package to stamp an $ORIGIN
    RUNPATH ships it.
    """
    assert "binary-or-shlib-defines-rpath" in LINTER.read_text(encoding="utf-8")


def test_quickshell_is_not_exempted_from_the_linter():
    """The fix must be at the source, not a per-package exemption."""
    assert "quickshell" not in LINTER.read_text(encoding="utf-8").lower()
