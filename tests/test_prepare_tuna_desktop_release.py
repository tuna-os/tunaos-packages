"""The tuna-desktop recipe tracks one upstream tag, and the fill-in script keeps it that way."""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_tuna_desktop_release", ROOT / "scripts" / "prepare-tuna-desktop-release.py"
)
assert SPEC and SPEC.loader
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)

RECIPE = ROOT / "packages" / "tuna-desktop" / "package.yaml"
SOURCE_SHA = "1" * 64
VENDOR_SHA = "2" * 64


@pytest.mark.parametrize(
    ("tag", "semver", "version"),
    [("v1.0.0-beta.1", "1.0.0-beta.1", "1.0.0~beta.1"), ("v1.0.0", "1.0.0", "1.0.0"), ("v1.2.3-rc.2", "1.2.3-rc.2", "1.2.3~rc.2")],
)
def test_tag_maps_to_reported_and_recipe_versions(tag: str, semver: str, version: str) -> None:
    assert prepare.parse_tag(tag) == (semver, version)


@pytest.mark.parametrize("tag", ["1.0.0", "v1.0", "v1.0.0-1", "v1.0.0_beta", "v1.0.0-beta-1"])
def test_tag_rejects_forms_the_recipe_cannot_carry(tag: str) -> None:
    with pytest.raises(SystemExit):
        prepare.parse_tag(tag)


def test_checked_in_recipe_agrees_on_one_tag() -> None:
    """version, TUNA_VERSION, smoke want=, source and vendor asset name one tag."""
    prepare.check_recipe(yaml.safe_load(RECIPE.read_text()))


def test_rewrite_fills_placeholders_for_the_tag() -> None:
    text = prepare.rewrite_recipe(RECIPE.read_text(), "v1.0.0-beta.1", SOURCE_SHA, VENDOR_SHA)
    recipe = yaml.safe_load(text)
    prepare.check_recipe(recipe)
    assert "PLACEHOLDER" not in text and "0" * 64 not in text
    assert recipe["version"] == "1.0.0~beta.1"
    assert recipe["source"] == {
        "url": "https://github.com/tuna-os/tuna-desktop/archive/refs/tags/v1.0.0-beta.1.tar.gz",
        "sha256": SOURCE_SHA,
        "directory": "tuna-desktop-1.0.0-beta.1",
    }
    assert recipe["sources"][0]["url"].endswith("/tuna-desktop-vendor-v1.0.0-beta.1/tuna-desktop-vendor.tar.gz")
    assert recipe["sources"][0]["sha256"] == VENDOR_SHA
    assert recipe["build"]["environment"]["TUNA_VERSION"] == "v1.0.0-beta.1"


def test_rewrite_moves_to_a_later_tag_and_resets_release() -> None:
    beta = prepare.rewrite_recipe(RECIPE.read_text(), "v1.0.0-beta.1", SOURCE_SHA, VENDOR_SHA)
    beta = beta.replace("\nrelease: 1\n", "\nrelease: 3\n")
    final = prepare.rewrite_recipe(beta, "v1.0.0", "3" * 64, "4" * 64)
    recipe = yaml.safe_load(final)
    prepare.check_recipe(recipe)
    assert recipe["version"] == "1.0.0"
    assert recipe["release"] == 1
    assert "want=1.0.0;" in recipe["verify"]["smoke"]
    # Only the tracked fields move; every other line is byte-identical.
    assert len(final.splitlines()) == len(beta.splitlines())


def test_rewrite_refuses_placeholder_hashes() -> None:
    with pytest.raises(SystemExit):
        prepare.rewrite_recipe(RECIPE.read_text(), "v1.0.0-beta.1", "0" * 64, VENDOR_SHA)


def test_check_recipe_rejects_a_smoke_that_expects_another_version() -> None:
    recipe = yaml.safe_load(RECIPE.read_text())
    want = recipe["build"]["environment"]["TUNA_VERSION"].removeprefix("v")
    recipe["verify"]["smoke"] = recipe["verify"]["smoke"].replace(f"want={want};", "want=0.1.0;")
    with pytest.raises(SystemExit):
        prepare.check_recipe(recipe)


@pytest.fixture
def release_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Control subprocesses and public HTTP responses; never contact GitHub."""
    recipe = tmp_path / "package.yaml"
    original = RECIPE.read_text()
    recipe.write_text(original)
    asset = gzip.compress(b"fixed vendor fixture", mtime=0)
    state = SimpleNamespace(
        recipe=recipe, original=original, asset=asset, statuses=[404, 200],
        calls=[], downloads=[], packs=[], offline=[], upload_error=False,
        public_asset=asset,
    )
    monkeypatch.setattr(sys, "argv", [
        "prepare-tuna-desktop-release.py", "v1.0.0-beta.3",
        "--recipe", str(recipe), "--keep", str(tmp_path / "work"), "--publish",
    ])

    def http(request):
        state.downloads.append(request.full_url)
        return io.BytesIO(state.public_asset if "/releases/download/" in request.full_url else b"source fixture")

    def extract(archive, destination, top):
        destination.mkdir(parents=True, exist_ok=True)
        return destination, 123456

    def pack(tree, epoch, output):
        state.packs.append((tree, epoch, output))
        output.write_bytes(asset)

    def run(command, **kwargs):
        state.calls.append((command, kwargs))
        if command[0] != "gh":
            return subprocess.CompletedProcess(command, 0)
        assert command[:4] == ["gh", "api", "--hostname", "github.com"]
        assert "graphql" not in command and "release" not in command
        method = command[command.index("--method") + 1]
        if method == "GET":
            assert command[6] == "repos/tuna-os/tunaos-packages/releases/tags/tuna-desktop-vendor-v1.0.0-beta.3"
            assert command[7:] == ["--include", "--silent"]
            status = state.statuses.pop(0)
            return subprocess.CompletedProcess(command, 0 if status == 200 else 1,
                                               f"HTTP/2.0 {status} Test\r\nX-Test: fixture\r\n\r\n")
        if command[6] == "repos/tuna-os/tunaos-packages/releases":
            assert command[7:] == ["--input", "-"]
            state.payload = json.loads(kwargs["input"])
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "id": 42,
                "upload_url": "https://uploads.github.com/repos/tuna-os/tunaos-packages/releases/42/assets{?name,label}",
            }))
        assert command[6] == (
            "https://uploads.github.com/repos/tuna-os/tunaos-packages/releases/42/assets"
            "?name=tuna-desktop-vendor.tar.gz"
        )
        assert command[7:9] == ["--header", "Content-Type: application/gzip"]
        assert command[9] == "--input" and len(command) == 11
        assert Path(command[10]).read_bytes() == asset
        assert kwargs["check"] is True
        if state.upload_error:
            raise subprocess.CalledProcessError(422, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(prepare, "urlopen", http)
    monkeypatch.setattr(prepare, "extract", extract)
    monkeypatch.setattr(prepare, "pack_vendor", pack)
    monkeypatch.setattr(prepare, "offline_metadata", lambda *args: state.offline.append(args))
    monkeypatch.setattr(prepare.subprocess, "run", run)
    return state


def test_rest_create_upload_and_public_hash_before_six_field_rewrite(release_flow) -> None:
    state = release_flow
    assert prepare.main() == 0
    assert state.payload == {
        "tag_name": "tuna-desktop-vendor-v1.0.0-beta.3",
        "name": "tuna-desktop vendor v1.0.0-beta.3",
        "body": (
            "Immutable offline Cargo dependencies for Tuna Desktop tag v1.0.0-beta.3 (tuna-os/tuna-desktop). "
            "Generated with `cargo vendor --locked --versioned-dirs`, packed as a deterministic tar "
            "(sorted names, uid/gid 0, normalized modes, the tag commit's time as mtime, `gzip -n -9`) "
            "by scripts/prepare-tuna-desktop-release.py. Offline locked metadata resolution "
            "(`cargo metadata --locked --offline`) passed against the vendor directory. This asset "
            "contains source dependencies, not an installable desktop release.\n\n"
            f"SHA-256: `{hashlib.sha256(state.asset).hexdigest()}`."
        ),
        "draft": False, "prerelease": False, "make_latest": "false",
    }
    assert state.downloads == [
        prepare.source_url("v1.0.0-beta.3"), prepare.vendor_url("v1.0.0-beta.3"),
    ]
    assert state.recipe.read_text() == prepare.rewrite_recipe(
        state.original, "v1.0.0-beta.3", hashlib.sha256(b"source fixture").hexdigest(),
        hashlib.sha256(state.asset).hexdigest(),
    )
    assert len(state.packs) == 2
    assert state.packs[0][:2] == state.packs[1][:2]
    assert len(state.offline) == 1
    assert state.offline[0][3] == yaml.safe_load(state.original)["build"]["cargo_config"]
    assert state.calls[0][0] == ["cargo", "vendor", "--locked", "--versioned-dirs", "vendor"]


def test_existing_release_only_uses_rest_get_and_public_download(release_flow) -> None:
    release_flow.statuses = [200]
    assert prepare.main() == 0
    assert len([cmd for cmd, _ in release_flow.calls if cmd[0] == "gh"]) == 1
    assert release_flow.downloads[-1] == prepare.vendor_url("v1.0.0-beta.3")


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
def test_lookup_errors_are_fatal_without_creation_or_recipe_mutation(release_flow, status) -> None:
    release_flow.statuses = [status]
    with pytest.raises(SystemExit, match=f"lookup failed.*HTTP {status}"):
        prepare.main()
    assert release_flow.recipe.read_text() == release_flow.original
    assert len([cmd for cmd, _ in release_flow.calls if cmd[0] == "gh"]) == 1
    assert len(release_flow.downloads) == 1


def test_404_without_publish_does_not_mutate_recipe(release_flow, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", sys.argv[:-1])
    with pytest.raises(SystemExit, match="does not exist"):
        prepare.main()
    assert release_flow.recipe.read_text() == release_flow.original
    assert len([cmd for cmd, _ in release_flow.calls if cmd[0] == "gh"]) == 1


def test_upload_failure_is_not_retried_and_does_not_mutate_recipe(release_flow) -> None:
    release_flow.upload_error = True
    with pytest.raises(subprocess.CalledProcessError):
        prepare.main()
    assert release_flow.recipe.read_text() == release_flow.original
    assert len([cmd for cmd, _ in release_flow.calls if cmd[0] == "gh"]) == 3
    assert len(release_flow.downloads) == 1


@pytest.mark.parametrize("statuses", [[200], [404, 200]])
def test_public_asset_mismatch_does_not_mutate_recipe(release_flow, statuses) -> None:
    release_flow.statuses = statuses.copy()
    release_flow.public_asset = b"different public asset"
    with pytest.raises(SystemExit, match="asset has sha256"):
        prepare.main()
    assert release_flow.recipe.read_text() == release_flow.original


def test_transport_failure_without_http_status_is_not_absence(release_flow, monkeypatch) -> None:
    monkeypatch.setattr(prepare.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, ""))
    with pytest.raises(SystemExit, match="HTTP unknown"):
        prepare.release_asset_sha("v1.0.0-beta.3", release_flow.recipe.parent)
    assert release_flow.recipe.read_text() == release_flow.original


def test_creation_failure_does_not_upload_or_mutate_recipe(release_flow, monkeypatch) -> None:
    controlled_run = prepare.subprocess.run

    def run(command, **kwargs):
        if command[0] == "gh" and command[6] == "repos/tuna-os/tunaos-packages/releases":
            raise subprocess.CalledProcessError(1, command)
        return controlled_run(command, **kwargs)

    monkeypatch.setattr(prepare.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        prepare.main()
    assert release_flow.recipe.read_text() == release_flow.original
    assert len(release_flow.downloads) == 1


def test_different_second_pack_stops_before_rest_or_recipe_write(release_flow, monkeypatch) -> None:
    def pack(tree, epoch, output):
        output.write_bytes(b"first" if output.name == prepare.VENDOR_FILENAME else b"second")

    monkeypatch.setattr(prepare, "pack_vendor", pack)
    with pytest.raises(SystemExit, match="not reproducible"):
        prepare.main()
    assert not any(cmd[0] == "gh" for cmd, _ in release_flow.calls)
    assert release_flow.recipe.read_text() == release_flow.original


def test_offline_failure_stops_before_rest_or_recipe_write(release_flow, monkeypatch) -> None:
    def offline(*args):
        raise subprocess.CalledProcessError(1, ["cargo", "metadata", "--locked", "--offline"])

    monkeypatch.setattr(prepare, "offline_metadata", offline)
    with pytest.raises(subprocess.CalledProcessError):
        prepare.main()
    assert not any(cmd[0] == "gh" for cmd, _ in release_flow.calls)
    assert release_flow.recipe.read_text() == release_flow.original


def test_pack_vendor_is_reproducible_and_preserves_normalized_content(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir(mode=0o700)
    # Deliberately create files out of lexical order with differing modes/times.
    for name, content, mode in [
        ("z.txt", b"last file\n", 0o600),
        ("a.txt", b"first file\n", 0o664),
        ("run", b"#!/bin/sh\nexit 0\n", 0o700),
    ]:
        path = vendor / name
        path.write_bytes(content)
        path.chmod(mode)
        os.utime(path, (42, 42))
    first, second = tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"
    prepare.pack_vendor(tmp_path, 123456, first)
    (vendor / "z.txt").chmod(0o666)
    (vendor / "run").chmod(0o775)
    vendor.chmod(0o777)
    for path in [vendor, *vendor.iterdir()]:
        os.utime(path, (987654, 987654))
    prepare.pack_vendor(tmp_path, 123456, second)
    assert first.read_bytes() == second.read_bytes()
    assert prepare.sha256(first) == prepare.sha256(second)
    header = first.read_bytes()[:10]
    assert header[:3] == b"\x1f\x8b\x08"
    assert header[3] == 0  # gzip -n: no filename or other optional header fields.
    assert header[4:8] == b"\0" * 4
    assert header[8] == 2  # gzip's maximum-compression marker.
    with tarfile.open(first) as packed:
        members = packed.getmembers()
        assert [m.name for m in members] == ["vendor", "vendor/a.txt", "vendor/run", "vendor/z.txt"]
        for member in members:
            assert (member.uid, member.gid, member.uname, member.gname, member.mtime) == (0, 0, "", "", 123456)
            assert member.mode == (0o755 if member.isdir() or member.name == "vendor/run" else 0o644)
            if member.isfile():
                stream = packed.extractfile(member)
                assert stream is not None
                assert stream.read() == (tmp_path / member.name).read_bytes()
    (vendor / "a.txt").write_bytes(b"changed dependency source\n")
    prepare.pack_vendor(tmp_path, 123456, second)
    assert prepare.sha256(first) != prepare.sha256(second)


@pytest.mark.parametrize("dependency_state", ["intact", "removed", "corrupt"])
def test_real_offline_metadata_requires_valid_vendored_registry_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dependency_state: str,
) -> None:
    cargo_home = tmp_path / "empty-cargo-home"
    cargo_home.mkdir()
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    top = "tuna-desktop-1.0.0"
    source = tmp_path / top
    (source / "src").mkdir(parents=True)
    (source / "src" / "lib.rs").write_text("pub fn fixture() {}\n")
    (source / "Cargo.toml").write_text(
        '[package]\nname = "offline-fixture"\nversion = "0.1.0"\nedition = "2021"\n'
        '[dependencies]\ntuna-vendor-fixture = "=1.0.0"\n'
    )
    package_checksum = "a" * 64
    (source / "Cargo.lock").write_text(
        'version = 3\n\n[[package]]\nname = "offline-fixture"\nversion = "0.1.0"\n'
        'dependencies = ["tuna-vendor-fixture"]\n\n'
        '[[package]]\nname = "tuna-vendor-fixture"\nversion = "1.0.0"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
        f'checksum = "{package_checksum}"\n'
    )
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as packed:
        packed.add(source, arcname=top)
    dependency = source / "vendor" / "tuna-vendor-fixture-1.0.0"
    (dependency / "src").mkdir(parents=True)
    (dependency / "Cargo.toml").write_text(
        '[package]\nname = "tuna-vendor-fixture"\nversion = "1.0.0"\nedition = "2021"\n'
    )
    (dependency / "src" / "lib.rs").write_text("pub fn dependency() {}\n")
    (dependency / ".cargo-checksum.json").write_text(json.dumps({
        "package": package_checksum,
        "files": {
            name: hashlib.sha256((dependency / name).read_bytes()).hexdigest()
            for name in ["Cargo.toml", "src/lib.rs"]
        },
    }))
    if dependency_state == "removed":
        prepare.shutil.rmtree(dependency)
    elif dependency_state == "corrupt":
        # Break manifest parsing, which metadata must perform without compiling.
        (dependency / "Cargo.toml").write_text("this is not a Cargo manifest\n")
    vendor = tmp_path / "vendor.tar.gz"
    prepare.pack_vendor(source, 123456, vendor)
    config = yaml.safe_load(RECIPE.read_text())["build"]["cargo_config"]
    work = tmp_path / "offline"
    work.mkdir()
    stale = work / "stale-tree-marker"
    stale.write_text("must not survive fresh extraction")
    if dependency_state == "intact":
        prepare.offline_metadata(archive, vendor, top, config, work)
        assert not stale.exists()
        assert (work / top / ".cargo" / "config.toml").read_text() == config
        assert (work / top / "Cargo.lock").read_bytes() == (source / "Cargo.lock").read_bytes()
        assert not (work / top / "target").exists()
        # Cargo can initialize its cache marker even while resolving offline.
        assert not any(
            path.is_file() and path.name != "CACHEDIR.TAG"
            for cache in ("registry", "git")
            for path in (cargo_home / cache).rglob("*")
        )
    else:
        with pytest.raises(subprocess.CalledProcessError):
            prepare.offline_metadata(archive, vendor, top, config, work)
        assert not stale.exists()
        assert not (work / top / "target").exists()
