"""The hummingbird mock buildroot reads utah-packages out of its OCI image.

projectbluefin/utah-packages publishes no HTTP baseurl -- the repository
lives as /repository inside the image -- so the chain materialises it
before mock starts (scripts/materialize-consumed-repo.py) and the mock
configs point a file:// repo at the result. What is held here, against a
fake registry:

- the full tree lands on disk (RPMs, not just repodata), members outside
  repository/ and absolute/`..` escapes never do;
- provenance records the digest; a second run with the same ref is a
  cache hit without touching the registry;
- a tag ref, an unreachable registry, and a layer without repository/
  fail closed, never an empty repo;
- --allow-empty writes a repodata dnf accepts (missing arch, e.g.
  aarch64) instead of failing the arches utah does not publish.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import pathlib
import xml.etree.ElementTree as ET

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oci = load("oci_repository")

import sys as _sys

_sys.modules["oci_repository"] = oci
mat = load("materialize-consumed-repo")

REF = "oci://ghcr.io/projectbluefin/utah-packages@sha256:" + "9c" * 32

PRIMARY_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="1">
<package type="rpm"><name>gnome-shell</name><arch>x86_64</arch>
<version epoch="0" ver="51.0" rel="1.hum1.bfin"/>
<format></format></package>
</metadata>"""


def repomd(primary_name: str, sha: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo"><revision>1725000000</revision>
<data type="primary"><checksum type="sha256">{sha}</checksum><location href="repodata/{primary_name}"/></data>
</repomd>""".encode()


class FakeRegistry:
    def __init__(self, layer: bytes, digest: str):
        self.layer, self.digest = layer, digest
        self.hits: list[str] = []

    def __call__(self, request, timeout=0):
        url = request.full_url
        self.hits.append(url)
        if "/token?" in url:
            return io.BytesIO(json.dumps({"token": "anon"}).encode())
        if "/manifests/" in url:
            return io.BytesIO(json.dumps({
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "layers": [{"digest": "sha256:" + "c" * 64,
                            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}],
            }).encode())
        if "/blobs/" in url:
            return io.BytesIO(self.layer)
        raise AssertionError(f"unexpected fetch {url}")


def make_repo_layer() -> tuple[bytes, str]:
    primary_gz = gzip.compress(PRIMARY_XML)
    sha = hashlib.sha256(primary_gz).hexdigest()
    name = f"{sha}-primary.xml.gz"
    layer = oci.make_layer({
        "repository/gnome-shell-51.0-1.hum1.bfin.x86_64.rpm": b"fake rpm" * 100,
        f"repository/repodata/{name}": primary_gz,
        "repository/repodata/repomd.xml": repomd(name, sha),
        "README.md": b"not part of the repository",
        "/repository/absolute.rpm": b"must not escape",
        "repository/../escape.rpm": b"must not escape",
    })
    return layer, sha


def run_mat(monkeypatch, args: list[str]) -> int:
    import sys
    monkeypatch.setattr(sys, "argv", ["materialize-consumed-repo", *args])
    return mat.main()


def test_full_tree_materialises_and_escapes_do_not(tmp_path, monkeypatch):
    layer, _ = make_repo_layer()
    registry = FakeRegistry(layer, "sha256:" + "9c" * 32)
    monkeypatch.setattr(oci, "urlopen", registry)
    out = tmp_path / "utah"
    assert run_mat(monkeypatch, ["--ref", REF, "--arch", "x86_64",
                                 "--out", str(out),
                                 "--cache-dir", str(tmp_path / "cache")]) == 0
    assert (out / "repository" / "gnome-shell-51.0-1.hum1.bfin.x86_64.rpm").exists()
    assert (out / "repository" / "repodata" / "repomd.xml").exists()
    assert not (out / "README.md").exists()
    assert not (out / "absolute.rpm").exists()
    assert not (tmp_path / "escape.rpm").exists()
    prov = json.loads((out / ".provenance.json").read_text())
    assert prov["ref"] == REF and prov["arch"] == "x86_64"
    assert prov["files"] >= 3


def test_second_run_is_a_cache_hit(tmp_path, monkeypatch):
    layer, _ = make_repo_layer()
    registry = FakeRegistry(layer, "sha256:" + "9c" * 32)
    monkeypatch.setattr(oci, "urlopen", registry)
    args = ["--ref", REF, "--arch", "x86_64", "--out", str(tmp_path / "utah"),
            "--cache-dir", str(tmp_path / "cache")]
    assert run_mat(monkeypatch, args) == 0
    hits = len(registry.hits)
    assert run_mat(monkeypatch, args) == 0
    assert len(registry.hits) == hits


def test_tag_ref_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(oci, "urlopen", FakeRegistry(b"", "sha256:" + "9c" * 32))
    rc = run_mat(monkeypatch, ["--ref", "oci://ghcr.io/o/r:latest",
                               "--out", str(tmp_path / "utah")])
    assert rc != 0
    assert "ERROR" in capsys.readouterr().out


def test_layer_without_repository_fails_closed(tmp_path, monkeypatch, capsys):
    layer = oci.make_layer({"other/file.txt": b"x"})
    monkeypatch.setattr(oci, "urlopen", FakeRegistry(layer, "sha256:" + "9c" * 32))
    rc = run_mat(monkeypatch, ["--ref", REF, "--out", str(tmp_path / "utah"),
                               "--cache-dir", str(tmp_path / "cache")])
    assert rc != 0
    assert "ERROR" in capsys.readouterr().out
    assert not (tmp_path / "utah" / ".provenance.json").exists()


def test_allow_empty_writes_a_repo_dnf_accepts(tmp_path, monkeypatch):
    class NoArch(FakeRegistry):
        def __call__(self, request, timeout=0):
            url = request.full_url
            self.hits.append(url)
            if "/token?" in url:
                return io.BytesIO(json.dumps({"token": "anon"}).encode())
            if "/manifests/" in url:
                return io.BytesIO(json.dumps({
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "manifests": [{"digest": "sha256:" + "b" * 64,
                                   "platform": {"architecture": "amd64", "os": "linux"}}],
                }).encode())
            raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(oci, "urlopen", NoArch(b"", "sha256:" + "9c" * 32))
    out = tmp_path / "utah"
    rc = run_mat(monkeypatch, ["--ref", REF, "--arch", "aarch64",
                               "--out", str(out), "--allow-empty"])
    assert rc == 0
    repomd_xml = (out / "repository" / "repodata" / "repomd.xml").read_bytes()
    root = ET.fromstring(repomd_xml)
    ns = {"r": "http://linux.duke.edu/metadata/repo"}
    assert root.findtext("r:revision", namespaces=ns) == "0"
    prov = json.loads((out / ".provenance.json").read_text())
    assert prov["empty"] is True


def test_missing_arch_without_allow_empty_fails(tmp_path, monkeypatch):
    class NoArch(FakeRegistry):
        def __call__(self, request, timeout=0):
            url = request.full_url
            self.hits.append(url)
            if "/token?" in url:
                return io.BytesIO(json.dumps({"token": "anon"}).encode())
            if "/manifests/" in url:
                return io.BytesIO(json.dumps({
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "manifests": [],
                }).encode())
            raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(oci, "urlopen", NoArch(b"", "sha256:" + "9c" * 32))
    rc = run_mat(monkeypatch, ["--ref", REF, "--arch", "aarch64",
                               "--out", str(tmp_path / "utah")])
    assert rc != 0


def test_chain_band_mounts_utah_only_for_hummingbird():
    text = (ROOT / ".github" / "workflows" / "chain-band.yml").read_text()
    assert "UTAH_REPO_DIR" in text
    assert "materialize-consumed-repo.py" in text
    assert "inputs.target == 'hummingbird'" in text


def test_factory_cell_materialises_utah_for_hummingbird_build_chain():
    text = (ROOT / ".github" / "workflows" / "package-factory-cell.yml").read_text()
    assert "materialize-consumed-repo.py" in text
    assert "startsWith(matrix.mock_config, 'hummingbird')" in text
    assert "UTAH_REPO_DIR" in text
