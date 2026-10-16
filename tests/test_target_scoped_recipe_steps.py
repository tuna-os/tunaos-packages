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
    assert install.index("ln -s tuna-greeter") < install.index("patchelf --set-rpath")
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
    recipe = copy.deepcopy(TUNA)
    recipe["build"]["targets"]["noble"] = recipe["build"]["targets"].pop("trixie")
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)
    recipe = copy.deepcopy(TUNA)
    recipe["sources"][1]["targets"] = ["noble"]
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)


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
