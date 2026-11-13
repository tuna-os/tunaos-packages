"""Native Arch recipe admission, bounded rendering and complete split-artifact checks.

These tests do not build GDM, start PAM/D-Bus, or claim a real login proof.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tideforge = load_module("gdm_native_tideforge", ROOT / "scripts/tideforge.py")
metadata = load_module("gdm_native_metadata", ROOT / "scripts/validate-built-arch-package.py")


@pytest.fixture
def recipe():
    return yaml.safe_load((ROOT / "packages/gdm/package.yaml").read_text())


@pytest.fixture
def private_recipe(recipe, tmp_path, monkeypatch):
    native = recipe["outputs"]["arch"]["native_pkgbuild"]
    for item in [native, *native["files"]]:
        destination = tmp_path / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / item["path"]).read_bytes())
    monkeypatch.setattr(tideforge, "ROOT", tmp_path)
    return recipe, tmp_path


def test_real_recipe_validates_and_renders_native_bytes(recipe):
    tideforge.validate(recipe, "arch")
    native = recipe["outputs"]["arch"]["native_pkgbuild"]
    rendered = tideforge.render(recipe, "arch")
    assert set(rendered) == {"PKGBUILD", "0001-resolve-session-name-before-open.patch"}
    for item in [native, *native["files"]]:
        content = rendered[Path(item["path"]).name].encode()
        assert content == (ROOT / item["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == item["sha256"]


def test_native_input_contract_is_not_rendered_as_arch_metadata(recipe):
    assert tideforge.arch_metadata(recipe) == {}
    recipe["outputs"]["arch"]["backup"] = ["etc/pam.d/gdm-password"]
    recipe["files"]["common"].append("etc/pam.d/gdm-password")
    assert tideforge.arch_metadata(recipe) == {"backup": ["etc/pam.d/gdm-password"]}
    tideforge.validate(recipe, "arch")
    assert tideforge.render(recipe, "arch")["PKGBUILD"] == (ROOT / "packages/gdm/PKGBUILD").read_text()


def test_native_recipe_still_rejects_invalid_arch_metadata(recipe):
    recipe["outputs"]["arch"]["backup"] = ["/etc/pam.d/gdm-password"]
    with pytest.raises(SystemExit):
        tideforge.validate(recipe, "arch")


@pytest.mark.parametrize("defect", [
    "unknown-key", "extra-target", "wrong-pkgbuild-path", "wrong-patch-path",
    "parent-traversal", "duplicate-patch", "missing-input", "symlink-input",
    "pkgbuild-hash", "patch-hash", "empty-packages", "duplicate-package",
    "missing-main-package", "invalid-package", "duplicate-optional-package",
    "too-many-patches", "oversized-input", "non-utf8-input",
])
def test_native_recipe_rejects_invalid_or_unbounded_inputs(private_recipe, defect):
    recipe, root = private_recipe
    native = recipe["outputs"]["arch"]["native_pkgbuild"]
    if defect == "unknown-key":
        native["ignored_input"] = "unsafe"
    elif defect == "extra-target":
        recipe["targets"].append("ubuntu")
    elif defect == "wrong-pkgbuild-path":
        native["path"] = "packages/other/PKGBUILD"
    elif defect == "wrong-patch-path":
        native["files"][0]["path"] = "packages/other/patches/session.patch"
    elif defect == "parent-traversal":
        native["path"] = "packages/gdm/../gdm/PKGBUILD"
    elif defect == "duplicate-patch":
        native["files"].append(copy.deepcopy(native["files"][0]))
    elif defect == "missing-input":
        (root / native["path"]).unlink()
    elif defect == "symlink-input":
        path = root / native["path"]
        outside = root / "outside"
        path.replace(outside)
        path.symlink_to(outside)
    elif defect == "pkgbuild-hash":
        native["sha256"] = "0" * 64
    elif defect == "patch-hash":
        native["files"][0]["sha256"] = "0" * 64
    elif defect == "empty-packages":
        native["packages"] = []
    elif defect == "duplicate-package":
        native["packages"].append("libgdm")
    elif defect == "missing-main-package":
        native["packages"] = ["libgdm"]
    elif defect == "invalid-package":
        native["packages"].append("../private")
    elif defect == "duplicate-optional-package":
        native["optional_packages"].append("libgdm")
    elif defect == "too-many-patches":
        native["files"] *= tideforge.NATIVE_ARCH_PATCH_BOUND + 1
    elif defect == "oversized-input":
        (root / native["path"]).write_bytes(b"x" * (tideforge.NATIVE_ARCH_INPUT_BOUND + 1))
    elif defect == "non-utf8-input":
        content = b"\xff"
        (root / native["path"]).write_bytes(content)
        native["sha256"] = hashlib.sha256(content).hexdigest()
    with pytest.raises(SystemExit):
        tideforge.validate(recipe, "arch")


def test_render_refuses_tampered_input_without_prior_validate(private_recipe):
    recipe, root = private_recipe
    native = recipe["outputs"]["arch"]["native_pkgbuild"]
    (root / native["path"]).write_text("pkgname=not-gdm\n")
    with pytest.raises(SystemExit):
        tideforge.render(recipe, "arch")


def test_render_keeps_literal_newline_bytes(private_recipe):
    recipe, root = private_recipe
    native = recipe["outputs"]["arch"]["native_pkgbuild"]
    path = root / native["path"]
    content = path.read_bytes().replace(b"\n", b"\r\n")
    path.write_bytes(content)
    native["sha256"] = hashlib.sha256(content).hexdigest()
    assert tideforge.render(recipe, "arch")["PKGBUILD"].encode() == content


def package_info(name, version="51.1.1-1.1", architecture="x86_64", dependencies="glib2"):
    return (f"Name            : {name}\nVersion         : {version}\n"
            f"Architecture    : {architecture}\nDepends On      : {dependencies}\n")


def pair_info(dependencies="accountsservice glib2\n                  libgdm=51.1.1-1.1"):
    return package_info("gdm", dependencies=dependencies) + "\n" + package_info("libgdm")


@pytest.mark.parametrize("reverse,debug", [(False, False), (True, False), (False, True)])
def test_complete_pair_verifies_independently_of_archive_order(recipe, monkeypatch, reverse, debug):
    monkeypatch.setattr(metadata, "tideforge", tideforge)
    records = pair_info().strip().split("\n\n")
    if reverse:
        records.reverse()
    if debug:
        records.append(package_info("gdm-debug"))
    metadata.validate(recipe, "\n\n".join(records))


@pytest.mark.parametrize("defect", [
    "missing-library", "only-library", "duplicate-library", "unexpected-output",
    "main-version", "library-version", "main-release", "library-release",
    "unversioned-library-dependency", "different-library-dependency", "loose-library-dependency",
    "arm-library", "arm-main", "debug-version",
])
def test_split_artifact_verification_rejects_missing_or_mismatched_pair(recipe, monkeypatch, defect):
    monkeypatch.setattr(metadata, "tideforge", tideforge)
    main = package_info("gdm", dependencies="accountsservice libgdm=51.1.1-1.1")
    library = package_info("libgdm")
    extras = []
    if defect == "missing-library":
        library = ""
    elif defect == "only-library":
        main = ""
    elif defect == "duplicate-library":
        extras.append(library)
    elif defect == "unexpected-output":
        extras.append(package_info("not-gdm"))
    elif defect == "main-version":
        main = main.replace("51.1.1-1.1", "51.1.2-1.1", 1)
    elif defect == "library-version":
        library = library.replace("51.1.1-1.1", "51.1.2-1.1", 1)
    elif defect == "main-release":
        main = main.replace("51.1.1-1.1", "51.1.1-1", 1)
    elif defect == "library-release":
        library = library.replace("51.1.1-1.1", "51.1.1-1", 1)
    elif defect == "unversioned-library-dependency":
        main = main.replace("libgdm=51.1.1-1.1", "libgdm")
    elif defect == "different-library-dependency":
        main = main.replace("libgdm=51.1.1-1.1", "libgdm=51.1.1-1")
    elif defect == "loose-library-dependency":
        main = main.replace("libgdm=51.1.1-1.1", "libgdm>=51.1.1-1.1")
    elif defect == "arm-library":
        library = library.replace("x86_64", "aarch64")
    elif defect == "arm-main":
        main = main.replace("x86_64", "aarch64")
    elif defect == "debug-version":
        extras.append(package_info("gdm-debug", version="51.1.1-1"))
    info = "\n".join([main, library, *extras])
    with pytest.raises(SystemExit):
        metadata.validate(recipe, info)
