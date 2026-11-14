"""A custom recipe's one-target steps reach that target and no other (#836).

tuna-desktop builds a private gtk4-layer-shell on trixie only. The source,
the build step and the install step are scoped by target; a leak would put a
second gtk4-layer-shell into every other target's package.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tideforge", ROOT / "scripts" / "tideforge.py")
tideforge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tideforge)

TUNA = yaml.safe_load((ROOT / "packages" / "tuna-desktop" / "package.yaml").read_text(encoding="utf-8"))


def test_trixie_renders_the_private_library_and_sid_does_not():
    trixie = tideforge.render_deb(TUNA, "trixie")
    sid = tideforge.render_deb(TUNA, "debian")
    build, install = trixie["debian/tideforge-build.sh"], trixie["debian/tideforge-install.sh"]
    assert "meson setup .gtk4-layer-shell-build private/gtk4-layer-shell" in build
    # The private build runs before cargo, so pkg-config already sees it.
    assert build.index("PKG_CONFIG_PATH") < build.index("cargo build")
    assert install.index("cp -a share/tuna-desktop/icons") < install.index("patchelf --set-rpath")
    assert "libgtk4-layer-shell-dev" not in trixie["debian/control"]
    for rendered in (sid["debian/tideforge-build.sh"], sid["debian/tideforge-install.sh"]):
        assert "gtk4-layer-shell" not in rendered
        assert "patchelf" not in rendered


def test_other_formats_never_see_the_trixie_source():
    spec = tideforge.render_rpm(TUNA, "fedora")["tuna-desktop.spec"]
    pkgbuild = tideforge.render_pkgbuild(TUNA, "arch")["PKGBUILD"]
    assert "gtk4-layer-shell-1.3.0.tar.gz" not in spec
    assert "gtk4-layer-shell-1.3.0.tar.gz" not in pkgbuild
    assert "patchelf" not in spec and "patchelf" not in pkgbuild


def test_a_step_for_a_target_the_recipe_does_not_enable_is_rejected():
    # alma10 is a factory target tuna-desktop does not enable.
    recipe = copy.deepcopy(TUNA)
    recipe["build"]["targets"]["alma10"] = recipe["build"]["targets"].pop("trixie")
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)
    recipe = copy.deepcopy(TUNA)
    recipe["sources"][1]["targets"] = ["alma10"]
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)


def test_noble_uses_the_versioned_toolchain_and_the_factory_layer_shell():
    noble = tideforge.render_deb(TUNA, "noble")
    control, build = noble["debian/control"], noble["debian/tideforge-build.sh"]
    build_depends = next(line for line in control.splitlines() if line.startswith("Build-Depends:"))
    for name in ("rustc-1.97", "cargo-1.97", "libgtk4-layer-shell-dev (>= 1.1)", "libgtk-4-dev (>= 4.14)"):
        assert name in build_depends
    # Noble's unversioned rustc/cargo are 1.75: never ask for them.
    assert not {"rustc", "cargo"} & {item.strip().split(" ")[0] for item in build_depends.split(":", 1)[1].split(",")}
    assert build.index('export PATH="/usr/lib/rust-1.97/bin:$PATH"') < build.index("cargo build")
    assert "libgtk4-layer-shell0 (>= 1.1)" in control
    # No private copy: noble has no system gtk4-layer-shell to shadow.
    for rendered in (build, noble["debian/tideforge-install.sh"]):
        assert "gtk4-layer-shell-build" not in rendered and "patchelf" not in rendered
    assert noble["debian/changelog"].startswith("tuna-desktop (1.0.0~beta.3-1) noble;")


def test_ubuntu_noble_publishes_and_verifies_tuna_desktop():
    plan = json.loads(subprocess.run(
        [sys.executable, "scripts/plan-deb-publish.py", "--packages", "tuna-desktop", "--distros", "ubuntu-noble"],
        capture_output=True, text=True, cwd=ROOT, check=True,
    ).stdout)
    assert {row["target"] for row in plan["matrix"]["include"]} == {"noble"}
    assert {row["install"] for row in plan["verify_matrix"]["include"]} == {"tuna-desktop"}


def _tar(name: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    return buffer.getvalue()


@pytest.mark.parametrize(("target", "present"), [("trixie", True), ("debian", False)])
def test_deb_assembly_unpacks_a_scoped_source_only_for_its_target(tmp_path, target, present):
    primary, extra = _tar("hello-1/main.c"), _tar("lib-1/lib.c")
    cache = tmp_path / "home" / ".cache" / "tideforge" / "sources"
    cache.mkdir(parents=True)
    for payload in (primary, extra):
        (cache / hashlib.sha256(payload).hexdigest()).write_bytes(payload)
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(yaml.safe_dump({
        "name": "hello",
        "version": "1",
        "source": {"url": "https://example.com/p.tar.gz", "sha256": hashlib.sha256(primary).hexdigest(), "directory": "hello-1"},
        "sources": [{"url": "https://example.com/l.tar.gz", "sha256": hashlib.sha256(extra).hexdigest(),
                     "destination": "private/lib", "targets": ["trixie"]}],
    }))
    root = tmp_path / "root"
    (root / "rendered" / "debian").mkdir(parents=True)
    (root / "rendered" / "debian" / "rules").write_text("#!/usr/bin/make -f\n")
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "assemble-deb-source-tree.py"), str(recipe), str(root), target],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert (root / "source" / "hello-1" / "private" / "lib" / "lib.c").is_file() is present


def test_debian_trixie_publishes_and_verifies_its_install_name():
    plan = json.loads(subprocess.run(
        [sys.executable, "scripts/plan-deb-publish.py", "--packages", "tuna-desktop", "--distros", "debian-trixie"],
        capture_output=True, text=True, cwd=ROOT, check=True,
    ).stdout)
    assert {row["target"] for row in plan["matrix"]["include"]} == {"trixie"}
    assert {row["install"] for row in plan["verify_matrix"]["include"]} == {"tuna-desktop"}
    ubuntu = json.loads(subprocess.run(
        [sys.executable, "scripts/plan-deb-publish.py", "--packages", "cpptrace-devel", "--distros", "ubuntu"],
        capture_output=True, text=True, cwd=ROOT, check=True,
    ).stdout)
    assert {row["install"] for row in ubuntu["verify_matrix"]["include"]} == {"libcpptrace-dev"}
