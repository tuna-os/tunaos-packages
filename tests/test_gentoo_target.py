"""The gentoo target renders tuna-desktop into a Portage overlay (ADR 0002, #838).

The ebuild is generated from the recipe, so these pin the parts a Gentoo
consumer depends on: the PMS version and path guppy installs
(gui-wm/tuna-desktop-1.0.0_beta1), the pinned sources under collision-free
DISTDIR names, cargo.eclass with the recipe's Rust floor, the recipe's own
commands, and the gpkg wiring through the cell, cache and publisher.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("tideforge", ROOT / "scripts" / "tideforge.py")
tideforge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tideforge)

TUNA = yaml.safe_load((ROOT / "packages" / "tuna-desktop" / "package.yaml").read_text(encoding="utf-8"))
EBUILD_PATH = "gui-wm/tuna-desktop/tuna-desktop-1.0.0_beta1.ebuild"


@pytest.mark.parametrize(
    ("version", "expected"),
    [("1.0.0~beta.1", "1.0.0_beta1"), ("1.2.3", "1.2.3"), ("2.0~rc2", "2.0_rc2"), ("1.0~alpha", "1.0_alpha")],
)
def test_recipe_versions_take_their_pms_spelling(version, expected):
    assert tideforge.gentoo_version(version) == expected


@pytest.mark.parametrize("version", ["1.0.0~dev.1", "1.0.0+git5", "1.0a"])
def test_versions_without_a_pms_spelling_fail(version):
    with pytest.raises(SystemExit):
        tideforge.gentoo_version(version)


def test_release_above_one_is_a_gentoo_revision():
    recipe = dict(TUNA, release=3)
    assert tideforge.gentoo_pf(recipe) == "tuna-desktop-1.0.0_beta1-r2"


def test_tuna_desktop_renders_a_one_package_overlay():
    rendered = tideforge.render(TUNA, "gentoo")
    assert set(rendered) == {"profiles/repo_name", "metadata/layout.conf", EBUILD_PATH}
    assert rendered["profiles/repo_name"] == "tunaos\n"
    assert "masters = gentoo" in rendered["metadata/layout.conf"]


def test_the_ebuild_uses_cargo_eclass_at_the_recipe_floor():
    ebuild = tideforge.render(TUNA, "gentoo")[EBUILD_PATH]
    assert ebuild.index('RUST_MIN_VER="1.92"') < ebuild.index("inherit cargo")
    assert "${RUST_DEPEND}" in ebuild.split('BDEPEND="', 1)[1]
    assert 'LICENSE="GPL-3+"' in ebuild
    assert 'KEYWORDS="amd64"' in ebuild
    assert "cargo_gen_config" in ebuild
    assert "cargo_env bash -euo pipefail <<'TIDEFORGE_EOF'" in ebuild
    assert ">=gui-libs/gtk4-layer-shell-1.1" in ebuild


def test_sources_are_the_pinned_archives_under_versioned_distfile_names():
    ebuild = tideforge.render(TUNA, "gentoo")[EBUILD_PATH]
    assert (
        "tuna-desktop-vendor.tar.gz -> tuna-desktop-1.0.0_beta1-tuna-desktop-vendor.tar.gz" in ebuild
    )
    assert "v1.0.0-beta.1.tar.gz -> tuna-desktop-1.0.0_beta1-v1.0.0-beta.1.tar.gz" in ebuild
    # trixie's private gtk4-layer-shell never reaches Gentoo, which has 1.3.0.
    assert "gtk4-layer-shell-1.3.0.tar.gz" not in ebuild
    assert 'S="${WORKDIR}/tuna-desktop-1.0.0-beta.1"' in ebuild


def test_the_recipe_commands_run_unchanged_with_destdir_as_D():
    ebuild = tideforge.render(TUNA, "gentoo")[EBUILD_PATH]
    compile_phase = ebuild.split("src_compile() {", 1)[1].split("\n}\n", 1)[0]
    install_phase = ebuild.split("src_install() {", 1)[1].split("\n}\n", 1)[0]
    assert "export TUNA_VERSION=v1.0.0-beta.1" in compile_phase
    assert "cargo build --release --locked --offline" in compile_phase
    assert "ln -s tuna-greeter ${D}/usr/bin/roost-greeter" in install_phase
    assert "{destdir}" not in install_phase
    # files.common is exact on every format; Portage would compress the doc.
    assert "docompress -x /usr/share/doc/tuna-desktop/nested-session.md" in install_phase
    assert "meson setup" not in ebuild and "patchelf" not in ebuild


def test_a_gpkg_target_requires_a_category():
    recipe = copy.deepcopy(TUNA)
    del recipe["outputs"]["gpkg"]
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)


def test_only_custom_recipes_render_ebuilds():
    recipe = dict(copy.deepcopy(TUNA), build_system="cargo")
    with pytest.raises(SystemExit):
        tideforge.render_ebuild(recipe, "gentoo")


def test_the_cell_and_publisher_carry_gpkg_artifacts():
    for path in (
        ".github/workflows/package-factory-cell.yml",
        ".github/actions/tideforge-cached-cell/action.yml",
    ):
        assert "-name '*.gpkg.tar'" in (ROOT / path).read_text(encoding="utf-8"), path
    runner = (ROOT / "scripts" / "run-package-factory-cell.sh").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "verify-package-factory-cell.sh").read_text(encoding="utf-8")
    assert "\n  gpkg)\n" in runner and "gentoo-build-cell.sh" in runner
    assert "\n  gpkg)\n" in verify and "gentoo-clean-install.sh" in verify


def test_the_publish_plan_targets_the_contract_paths():
    plan = subprocess.run(
        [sys.executable, "scripts/plan-gentoo-publish.py", "--packages", "tuna-desktop"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    result = json.loads(plan.stdout)
    assert result["publish"]["include"] == [{
        "arch": "amd64",
        "src": "gentoo/binpkgs/amd64",
        "served": "https://repo.tunaos.org/gentoo/binpkgs/amd64/",
        "runner": "ubuntu-latest",
    }]
    (build,) = result["build"]["include"]
    assert build["atom"] == "gui-wm/tuna-desktop"
    assert build["cell_id"] == "tideforge-tuna-desktop-gentoo-amd64"
    assert "tuna-desktop" in result["overlay_packages"]
    assert result["overlay_url"] == "https://repo.tunaos.org/gentoo/overlay/tunaos-overlay.zip"


binhost = importlib.util.module_from_spec(
    spec := importlib.util.spec_from_file_location("gentoo_binhost", ROOT / "scripts" / "gentoo_binhost.py"))
spec.loader.exec_module(binhost)

# The shape emaint writes: a header stanza, then one stanza per gpkg.
PACKAGES = """\
ARCH: amd64
PACKAGES: 2
VERSION: 0

BUILD_ID: 1
CPV: gui-wm/tuna-desktop-1.0.0_beta1
PATH: gui-wm/tuna-desktop/tuna-desktop-1.0.0_beta1-1.gpkg.tar
RDEPEND: sys-apps/dbus dev-libs/glib:2 >=gui-libs/gtk-4.12:4[wayland] >=gui-libs/gtk4-layer-shell-1.1 || ( a/b c/d ) !x/blocked

BUILD_ID: 1
CPV: gui-wm/tuna-desktop-0.9.0
PATH: gui-wm/tuna-desktop/tuna-desktop-0.9.0-1.gpkg.tar
"""


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("1.0.0_beta1", "1.0.0", -1),
        ("1.0.0_rc1", "1.0.0_beta2", 1),
        ("1.0", "1.0_p1", -1),
        ("1.01", "1.1", -1),
        ("1.0-r1", "1.0", 1),
        ("1.0a", "1.0", 1),
        ("1.2.3", "1.2.3", 0),
    ],
)
def test_pms_version_ordering(left, right, expected):
    assert binhost.compare(left, right) == expected


def test_a_binhost_index_reads_into_the_standard_shape():
    index = binhost.parse_packages(PACKAGES)
    package = index["packages"]["gui-wm/tuna-desktop"]
    assert package["evr"] == "1.0.0_beta1"
    assert package["arch"] == "amd64"
    assert "gui-libs/gtk" in package["requires"]
    assert ("gui-libs/gtk4-layer-shell", ">=", "1.1") in package["requires_versioned"]
    # Alternatives and blockers are never hard requirements.
    assert not {"a/b", "c/d", "x/blocked"} & set(package["requires"])
    rows = list(binhost.iter_rows(PACKAGES))
    assert [row["evr"] for row in rows] == ["1.0.0_beta1", "0.9.0"]
    assert rows[0]["location"] == "gui-wm/tuna-desktop/tuna-desktop-1.0.0_beta1-1.gpkg.tar"


def test_satisfies_speaks_pms_operators():
    assert binhost.satisfies("1.3.0", ">=", "1.1")
    assert not binhost.satisfies("1.0.4", ">=", "1.1")
    assert binhost.satisfies("1.2.3-r2", "~", "1.2.3")
    assert binhost.satisfies("5.8.1", "=*", "5.8")
    assert not binhost.satisfies("5.80", "=*", "5.8")
