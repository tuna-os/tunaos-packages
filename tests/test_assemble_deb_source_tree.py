"""Tests for scripts/assemble-deb-source-tree.py.

The script assembles a Debian source tree for a rendered Tideforge recipe:
materialises the primary + auxiliary sources (cache-first, checksum
verified), extracts them with strip_components, copies rendered debian/
files over the in-tree ones, and writes source-dir. Run in a subprocess
against real temp files (no network: the cache is seeded directly, and
the script's fallback urlretrieve is exercised only when the cache
misses — which we avoid by pre-seeding).
"""

import copy
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "assemble-deb-source-tree.py"

PRIMARY = b"primary-tarball"
PRIMARY_SHA = hashlib.sha256(PRIMARY).hexdigest()
AUX = b"aux-tarball"
AUX_SHA = hashlib.sha256(AUX).hexdigest()


def _tar(payload, name):
    """Write a tar.gz containing one file at <name>; return the bytes."""
    import io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


# The cache is keyed by the sha256 of the ARCHIVE bytes (the recipe pin),
# so the primary/aux tarballs are materialised first and their sha used.
PRIMARY_TAR = _tar(PRIMARY, "hello-tuna-1.2.3/src/main.c")
PRIMARY_SHA = hashlib.sha256(PRIMARY_TAR).hexdigest()
AUX_TAR = _tar(AUX, "vendor/libfoo-2.0/foo.c")
AUX_SHA = hashlib.sha256(AUX_TAR).hexdigest()


def _seed_cache(cache_dir, sha, payload):
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / sha).write_bytes(payload)


def _run(tmp_path, recipe, home):
    recipe_path = tmp_path / "recipe.yaml"
    recipe_path.write_text(recipe)
    root = tmp_path / "root"
    root.mkdir()
    # rendered/debian lives in <root>/rendered — the script copies it over.
    rendered = root / "rendered" / "debian"
    rendered.mkdir(parents=True)
    (rendered / "rules").write_text("#!/usr/bin/make -f\n")
    (rendered / "control").write_text("Source: hello-tuna\n")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(recipe_path), str(root)],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    return result, root


BASE_RECIPE = """name: hello-tuna
version: '1.2.3'
source:
  url: https://example.com/primary.tar.gz
  sha256: {primary}
  directory: hello-tuna-1.2.3
"""


@pytest.fixture
def home_and_cache(tmp_path):
    home = tmp_path / "home"
    cache = home / ".cache" / "tideforge" / "sources"
    return home, cache


class TestAssembleDeb:
    def test_extracts_primary_and_copies_debian(self, tmp_path, home_and_cache):
        home, cache = home_and_cache
        _seed_cache(cache, PRIMARY_SHA, PRIMARY_TAR)

        result, root = _run(tmp_path, BASE_RECIPE.format(primary=PRIMARY_SHA), home)
        assert result.returncode == 0, result.stderr

        source = root / "source" / "hello-tuna-1.2.3"
        assert (source / "src" / "main.c").is_file()
        # rendered debian/ files copied over
        assert (source / "debian" / "control").is_file()
        assert (source / "debian" / "rules").is_file()
        assert (source / "debian" / "rules").stat().st_mode & 0o111, "rules must be executable"
        # source-dir points at the extracted root
        assert (root / "source-dir").read_text().strip() == "/work/source/hello-tuna-1.2.3"
        # in-tree debian/ cleared before copy (pop-os collision regression)
        assert "debian" in [p.name for p in source.iterdir()]

    def test_cache_hit_prints(self, tmp_path, home_and_cache):
        home, cache = home_and_cache
        _seed_cache(cache, PRIMARY_SHA, PRIMARY_TAR)

        result, _ = _run(tmp_path, BASE_RECIPE.format(primary=PRIMARY_SHA), home)
        assert result.returncode == 0, result.stderr
        assert "cache hit" in result.stdout

    def test_checksum_mismatch_rejected(self, tmp_path, home_and_cache):
        # Seed the cache with the WRONG bytes under the pinned sha — the
        # cache lookup is keyed by sha and validates on read, so a corrupted
        # blob must be treated as a miss and downloaded; with no network the
        # urlretrieve raises, which is the point (never ship bad bytes).
        home, cache = home_and_cache
        _seed_cache(cache, PRIMARY_SHA, b"corrupted")

        result, _ = _run(tmp_path, BASE_RECIPE.format(primary=PRIMARY_SHA), home)
        assert result.returncode != 0
        assert "checksum mismatch" in (result.stdout + result.stderr) or \
            "urlopen" in (result.stdout + result.stderr) or \
            "URLError" in (result.stderr)

    def test_auxiliary_source_with_strip_components(self, tmp_path, home_and_cache):
        home, cache = home_and_cache
        _seed_cache(cache, PRIMARY_SHA, PRIMARY_TAR)
        _seed_cache(cache, AUX_SHA, AUX_TAR)

        recipe = BASE_RECIPE.format(primary=PRIMARY_SHA) + f"""
sources:
  - url: https://example.com/aux.tar.gz
    sha256: {AUX_SHA}
    destination: vendor
    strip_components: 1
"""

        result, root = _run(tmp_path, recipe, home)
        assert result.returncode == 0, result.stderr
        source = root / "source" / "hello-tuna-1.2.3"
        assert (source / "vendor" / "libfoo-2.0" / "foo.c").is_file(), \
            "strip_components=1 keeps the inner dir; only the top level is stripped"

    def test_auxiliary_source_without_extract_copies_raw(self, tmp_path, home_and_cache):
        home, cache = home_and_cache
        _seed_cache(cache, PRIMARY_SHA, PRIMARY_TAR)
        aux_raw_sha = hashlib.sha256(AUX).hexdigest()
        _seed_cache(cache, aux_raw_sha, AUX)  # raw bytes, not a tarball

        recipe = BASE_RECIPE.format(primary=PRIMARY_SHA) + f"""
sources:
  - url: https://example.com/aux.bin
    sha256: {aux_raw_sha}
    destination: vendor/aux.bin
    extract: false
"""

        result, root = _run(tmp_path, recipe, home)
        assert result.returncode == 0, result.stderr
        source = root / "source" / "hello-tuna-1.2.3"
        assert (source / "vendor" / "aux.bin").read_bytes() == AUX


def _checked(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _archive_tree(directory, destination, names):
    with tarfile.open(destination, "w:xz") as archive:
        for name in names:
            archive.add(directory / name, arcname=name)


@pytest.fixture(scope="module")
def native_project(tmp_path_factory):
    """Real quilt source closure, genuinely signed with an isolated test key."""
    project = tmp_path_factory.mktemp("native-deb")
    scripts = project / "scripts"
    scripts.mkdir()
    for name in ("tideforge.py", "tideforge_cache.py", "assemble-deb-source-tree.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    (project / "manifests").mkdir()
    shutil.copyfile(ROOT / "manifests/package-factory.yaml",
                    project / "manifests/package-factory.yaml")
    upstream = project / "hello-tuna-1.2.3"
    upstream.mkdir()
    (upstream / "message").write_text("upstream\n")
    orig = project / "hello-tuna_1.2.3.orig.tar.xz"
    _archive_tree(project, orig, [upstream.name])
    debian = upstream / "debian"
    (debian / "source").mkdir(parents=True)
    (debian / "patches").mkdir()
    (debian / "source/format").write_text("3.0 (quilt)\n")
    (debian / "control").write_text(
        "Source: hello-tuna\nSection: misc\nPriority: optional\n"
        "Maintainer: Native Maintainer <native@example.invalid>\n"
        "Standards-Version: 4.6.2\n\n"
        "Package: hello-native\nArchitecture: all\n"
        "Description: Native rule output, not the generated package\n"
    )
    (debian / "changelog").write_text(
        "hello-tuna (1.2.3-0ubuntu1) resolute; urgency=medium\n\n"
        "  * Native packaging fixture.\n\n"
        " -- Native Maintainer <native@example.invalid>  Sat, 31 Oct 2026 00:00:00 +0000\n"
    )
    (debian / "rules").write_text(
        "#!/usr/bin/make -f\n"
        ".PHONY: clean build binary binary-indep binary-arch\n"
        "clean:\n\trm -rf debian/hello-native debian/files\n"
        "build:\n"
        "binary binary-indep: build\n"
        "\tinstall -d debian/hello-native/DEBIAN debian/hello-native/usr/share/hello-native\n"
        "\ttr '[:lower:]' '[:upper:]' < message "
        "> debian/hello-native/usr/share/hello-native/message\n"
        "\tinstall -m755 debian/postinst debian/hello-native/DEBIAN/postinst\n"
        "\tdpkg-gencontrol -phello-native -Pdebian/hello-native\n"
        "\tdpkg-deb --build --root-owner-group debian/hello-native ..\n"
        "binary-arch:\n"
    )
    (debian / "rules").chmod(0o755)
    (debian / "postinst").write_text("#!/bin/sh\nset -eu\nexit 0\n")
    (debian / "patches/ubuntu.patch").write_text(
        "--- a/message\n+++ b/message\n@@ -1 +1 @@\n-upstream\n+ubuntu\n"
    )
    (debian / "patches/series").write_text("ubuntu.patch\n")
    original_packaging = {
        path.relative_to(debian).as_posix(): path.read_bytes()
        for path in debian.rglob("*") if path.is_file()
    }
    _checked(["dpkg-source", "-b", str(upstream)], cwd=project)
    descriptor = project / "hello-tuna_1.2.3-0ubuntu1.dsc"
    unsigned = descriptor.read_text()
    keyhome = project / "gnupg"
    keyhome.mkdir(mode=0o700)
    _checked(["gpg", "--homedir", str(keyhome), "--batch", "--pinentry-mode", "loopback",
              "--passphrase", "", "--quick-generate-key",
              "Native Fixture <fixture@example.invalid>", "ed25519", "sign", "0"])

    def sign(text):
        return _checked(
            ["gpg", "--homedir", str(keyhome), "--batch", "--yes", "--armor",
             "--clearsign"], input=text,
        ).encode()

    patch = project / "packages/hello-tuna/patches/tuna.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("--- a/message\n+++ b/message\n@@ -1 +1 @@\n-ubuntu\n+tuna\n")
    debtar = project / "hello-tuna_1.2.3-0ubuntu1.debian.tar.xz"
    blobs = {orig.name: orig.read_bytes(), debtar.name: debtar.read_bytes(),
             descriptor.name: sign(unsigned)}
    recipe = {
        "schema": 1, "name": "hello-tuna", "version": "1.2.3",
        "release": "0ubuntu1+tuna1", "summary": "Fixture",
        "description": "Native source assembly fixture.", "license": "MIT",
        "targets": ["ubuntu"], "build_system": "data",
        "files": {"common": ["usr/share/hello-tuna/message"]},
        "install": {
            "files": [{"source": "message", "destination": "usr/share/hello-tuna/message"}]
        },
        "source": {
            "url": f"https://example.invalid/{orig.name}",
            "sha256": hashlib.sha256(blobs[orig.name]).hexdigest(),
            "directory": upstream.name,
        },
        "sources": [
            {
                "url": f"https://example.invalid/{debtar.name}",
                "sha256": hashlib.sha256(blobs[debtar.name]).hexdigest(),
                "destination": ".", "strip_components": 0,
            },
            {
                "url": f"https://example.invalid/{descriptor.name}",
                "sha256": hashlib.sha256(blobs[descriptor.name]).hexdigest(),
                "destination": f".tuna-source/{descriptor.name}", "extract": False,
            },
        ],
        "outputs": {"deb": {"native_source": {
            "version": "1.2.3-0ubuntu1", "suite": "resolute",
            "dsc": descriptor.name, "debian_tar": debtar.name,
            "patch": {"path": patch.relative_to(project).as_posix(),
                      "sha256": hashlib.sha256(patch.read_bytes()).hexdigest()},
        }}},
    }
    return project, recipe, blobs, original_packaging, unsigned, sign


@pytest.fixture
def native_case(tmp_path, native_project):
    project, recipe, blobs, packaging, unsigned, sign = native_project
    recipe = copy.deepcopy(recipe)
    home = tmp_path / "home"
    cache = home / ".cache/tideforge/sources"
    for name, blob in blobs.items():
        _seed_cache(cache, hashlib.sha256(blob).hexdigest(), blob)
    root = tmp_path / "root"
    root.mkdir()
    recipe_path = tmp_path / "recipe.yaml"
    env = dict(os.environ, HOME=str(home))

    def assemble(*, render=True):
        recipe_path.write_text(yaml.safe_dump(recipe))
        if render:
            _checked([sys.executable, str(project / "scripts/tideforge.py"), "render",
                      str(recipe_path), "--target", "ubuntu", "--output", str(root / "rendered")],
                     cwd=project, env=env)
        return subprocess.run(
            [sys.executable, str(project / "scripts/assemble-deb-source-tree.py"),
             str(recipe_path), str(root), "ubuntu"],
            cwd=project, env=env, capture_output=True, text=True,
        )

    return recipe, root, assemble, cache, blobs, packaging, unsigned, sign


def _assert_deb(package, destination, name, version, payload_path, payload):
    assert _checked(["dpkg-deb", "-f", str(package), "Package"]).strip() == name
    assert _checked(["dpkg-deb", "-f", str(package), "Version"]).strip() == version
    _checked(["dpkg-deb", "-x", str(package), str(destination)])
    assert (destination / payload_path).read_bytes() == payload


def test_native_packaging_executes_original_rules_and_both_quilt_patches(native_case, tmp_path):
    recipe, root, assemble, _, blobs, packaging, _, _ = native_case
    result = assemble()
    assert result.returncode == 0, result.stdout + result.stderr
    source = root / "source/hello-tuna-1.2.3"
    assert (root / "source-dir").read_text().strip() == "/work/source/hello-tuna-1.2.3"
    for name, content in packaging.items():
        if name not in {"changelog", "patches/series"}:
            assert (source / "debian" / name).read_bytes() == content
    assert (source / "debian/patches/series").read_text() == "ubuntu.patch\ntuna.patch\n"
    assert (source / "debian/changelog").read_bytes().endswith(packaging["changelog"])
    for name, blob in blobs.items():
        assert (root / "source/original-source" / name).read_bytes() == blob
    _checked(["dpkg-source", "--before-build", str(source)])
    _checked(["make", "-f", "debian/rules", "binary"], cwd=source)
    package, = (root / "source").glob("hello-native_*.deb")
    _assert_deb(package, tmp_path / "unpacked", "hello-native", "1.2.3-0ubuntu1+tuna1",
                "usr/share/hello-native/message", b"TUNA\n")
    control = tmp_path / "control"
    _checked(["dpkg-deb", "-e", str(package), str(control)])
    assert (control / "postinst").read_bytes() == packaging["postinst"]
    assert _checked(["dpkg-deb", "-f", str(package), "Maintainer"]).strip() == \
        "Native Maintainer <native@example.invalid>"
    # A downstream source build must remain a valid quilt source package too.
    _checked(["dpkg-source", "-b", source.name], cwd=source.parent)
    rebuilt = root / "source/hello-tuna_1.2.3-0ubuntu1+tuna1.dsc"
    extracted = tmp_path / "rebuilt-source"
    _checked(["dpkg-source", "-x", str(rebuilt), str(extracted)])
    assert (extracted / "message").read_bytes() == b"tuna\n"
    assert _checked(["dpkg-parsechangelog", "-l", str(extracted / "debian/changelog"),
                     "-S", "Version"]).strip() == "1.2.3-0ubuntu1+tuna1"


@pytest.mark.parametrize("defect,expected", [
    ("identity", "dsc identity"),
    ("closure", "dsc closure"),
    ("checksum", "dsc checksum/size"),
    ("unsigned", "not a signed source descriptor"),
])
def test_native_rejects_bad_descriptor_before_packaging_mutation(native_case, defect, expected):
    recipe, root, assemble, cache, _, packaging, unsigned, sign = native_case
    if defect == "identity":
        text = unsigned.replace("Version: 1.2.3-0ubuntu1", "Version: 1.2.3-0ubuntu2")
    elif defect == "closure":
        text = unsigned.replace("hello-tuna_1.2.3.orig.tar.xz", "unexpected.orig.tar.xz")
    elif defect == "checksum":
        text = unsigned.replace(recipe["source"]["sha256"], "0" * 64)
    else:
        text = unsigned
    blob = text.encode() if defect == "unsigned" else sign(text)
    sha = hashlib.sha256(blob).hexdigest()
    recipe["sources"][1]["sha256"] = sha
    _seed_cache(cache, sha, blob)
    result = assemble()
    assert result.returncode != 0
    assert expected in result.stderr
    source = root / "source/hello-tuna-1.2.3"
    assert (source / "debian/changelog").read_bytes() == packaging["changelog"]
    assert (source / "debian/patches/series").read_bytes() == packaging["patches/series"]
    assert not (source / "debian/patches/tuna.patch").exists()


def test_native_rejects_rendered_patch_digest_mismatch(native_case, native_project):
    _, root, assemble, _, _, packaging, _, _ = native_case
    # Render the real pinned patch, then corrupt only the consumer's rendered artifact.
    recipe_path = root.parent / "recipe.yaml"
    recipe = native_case[0]
    recipe_path.write_text(yaml.safe_dump(recipe))
    project = native_project[0]
    _checked([sys.executable, str(project / "scripts/tideforge.py"), "render", str(recipe_path),
              "--target", "ubuntu", "--output", str(root / "rendered")], cwd=project)
    (root / "rendered/debian/patches/tuna.patch").write_text("corrupted\n")
    result = assemble(render=False)
    assert result.returncode != 0
    assert "rendered native patch checksum mismatch" in result.stderr
    assert (
        root / "source/hello-tuna-1.2.3/debian/changelog"
    ).read_bytes() == packaging["changelog"]


def test_generated_mode_still_replaces_native_rules_and_builds_generated_package(
    native_case, tmp_path
):
    recipe, root, assemble, _, _, _, _, _ = native_case
    del recipe["outputs"]["deb"]["native_source"]
    recipe["release"] = 1
    result = assemble()
    assert result.returncode == 0, result.stdout + result.stderr
    source = root / "source/hello-tuna-1.2.3"
    _checked(["fakeroot", "make", "-f", "debian/rules", "binary"], cwd=source)
    package, = (root / "source").glob("hello-tuna_*.deb")
    _assert_deb(package, tmp_path / "generated", "hello-tuna", "1.2.3-1",
                "usr/share/hello-tuna/message", b"upstream\n")
    assert not (source / "debian/postinst").exists()
    assert not list((root / "source").glob("hello-native_*.deb"))


@pytest.mark.parametrize("field,value", [("version", "1.2.4-0ubuntu1"),
                                        ("release", "0ubuntu1+tuna0")])
def test_native_rejects_invalid_version_before_extraction(native_case, field, value):
    recipe, root, assemble, _, _, _, _, _ = native_case
    if field == "version":
        recipe["outputs"]["deb"]["native_source"]["version"] = value
    else:
        recipe[field] = value
    result = assemble(render=False)
    assert result.returncode != 0
    assert "native source" in result.stderr
    assert not (root / "source").exists()
