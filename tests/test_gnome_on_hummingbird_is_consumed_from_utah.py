"""GNOME on Hummingbird is consumed from projectbluefin/utah-packages, not built.

#629 / docs/HUMMINGBIRD-TARGET.md §7: Bluefin builds GNOME 51 for Hummingbird
in Hummingbird's own root and ships it as an OCI image whose one layer is a
createrepo_c tree. tunaOS's hummingbird:gnome consumes that image by digest.
For this factory that repository is part of what the target ALREADY HAS:
the gap engine must not order a second GNOME, and the installability walk
must enable it. Both need utah's primary.xml, which lives inside the image
layer and behind no HTTP baseurl -- hence scripts/oci_repository.py.

What is held here, and the failure each guards against:

- the catalog says `consumed_from: utah-packages` and names no source, and
  the validator accepts exactly that shape (consumed XOR sources, id known);
- the hummingbird contract declares the consumed index as a DIGEST
  reference (a tag would let the measurement drift from what tunaOS pins);
- the OCI reader finds repomd.xml and the primary it names inside a
  streamed layer, records the digest as provenance, caches by digest, and
  refuses tags -- run against a fake registry, so the token/manifest/blob
  walk is exercised, not described;
- `consumed_indexes()` folds the repository's provides into `have`, so a
  root utah ships is not "to build";
- the installability check enables the consumed repo and attributes a
  needer that lives there to `consumed`, not `target`.
"""
from __future__ import annotations

import gzip
import importlib.util
import io
import json
import pathlib
import subprocess
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
FACTORY = ROOT / "manifests" / "package-factory.yaml"
CATALOG = ROOT / "manifests" / "hummingbird-desktops.yaml"
VALIDATOR = ROOT / "scripts" / "validate-hummingbird-catalog.py"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oci = load("oci_repository")
gap = load("gap_engine")
_spec = importlib.util.spec_from_file_location(
    "check_hummingbird_installability", ROOT / "scripts" / "check-hummingbird-installability.py")
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)


# ── the declarations ───────────────────────────────────────────────────────


def hummingbird_measurement() -> dict:
    return yaml.safe_load(FACTORY.read_text())["targets"]["hummingbird"]["gap_measurement"]


def test_the_catalog_consumes_gnome_and_builds_the_rest():
    desktops = yaml.safe_load(CATALOG.read_text())["desktops"]
    assert desktops["gnome"].get("consumed_from") == "utah-packages"
    assert "sources" not in desktops["gnome"], "consumed and built at once is the drift #629 ends"
    assert desktops["gnome"]["required_packages"], "the roots stay declared: the audit and the walk still cover them"
    for other in ("kde", "cosmic", "niri", "xfce"):
        assert desktops[other].get("sources"), f"{other} is still built here"
        assert "consumed_from" not in desktops[other]


def test_the_contract_pins_utah_by_digest():
    consumed = hummingbird_measurement()["consumed_indexes"]
    assert [c["id"] for c in consumed] == ["utah-packages"]
    ref = consumed[0]["index"]
    assert ref.startswith("oci://ghcr.io/projectbluefin/utah-packages@sha256:"), ref
    oci.parse_ref(ref)  # raises on anything but a digest reference
    assert consumed[0].get("serves") == ["gnome"]


def run_validator(tmp_path, catalog: dict, factory: dict) -> subprocess.CompletedProcess:
    (tmp_path / "manifests").mkdir(parents=True)
    (tmp_path / "src" / "x").mkdir(parents=True)
    cat = tmp_path / "manifests" / "hummingbird-desktops.yaml"
    cat.write_text(yaml.safe_dump(catalog))
    fac = tmp_path / "manifests" / "package-factory.yaml"
    fac.write_text(yaml.safe_dump(factory))
    return subprocess.run([sys.executable, str(VALIDATOR), str(cat)], capture_output=True, text=True)


BASE = {"schema": 1,
        "target": {"id": "hummingbird-x", "baseurl": "https://t/", "r2_path": "h/x", "dist": ".bfin1"}}
FAC = {"targets": {"hummingbird": {"gap_measurement": {"consumed_indexes": [
    {"id": "utah-packages", "index": "oci://ghcr.io/o/r@sha256:" + "0" * 64}]}}}}


def test_the_validator_accepts_consumed_and_rejects_the_two_wrong_shapes(tmp_path):
    good = {**BASE, "desktops": {"gnome": {"required_packages": ["gdm"], "install_packages": ["gdm"],
                                          "consumed_from": "utah-packages"}}}
    assert run_validator(tmp_path / "a", good, FAC).returncode == 0

    both = {**BASE, "desktops": {"gnome": {"required_packages": ["gdm"], "install_packages": ["gdm"],
                                          "consumed_from": "utah-packages", "sources": [{"local": "src/x"}]}}}
    proc = run_validator(tmp_path / "b", both, FAC)
    assert proc.returncode != 0 and "exclusive" in proc.stderr

    unknown = {**BASE, "desktops": {"gnome": {"required_packages": ["gdm"], "install_packages": ["gdm"],
                                             "consumed_from": "somebody-else"}}}
    proc = run_validator(tmp_path / "c", unknown, FAC)
    assert proc.returncode != 0 and "somebody-else" in proc.stderr

    neither = {**BASE, "desktops": {"gnome": {"required_packages": ["gdm"], "install_packages": ["gdm"]}}}
    proc = run_validator(tmp_path / "d", neither, FAC)
    assert proc.returncode != 0 and "consumed_from" in proc.stderr


def test_the_real_catalog_validates_against_the_real_contract():
    proc = subprocess.run([sys.executable, str(VALIDATOR), str(CATALOG)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ── the OCI reader, against a fake registry ────────────────────────────────


PRIMARY_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="1">
<package type="rpm"><name>gnome-shell</name><arch>x86_64</arch>
<version epoch="0" ver="51.0" rel="1.hum1.bfin"/>
<format><rpm:provides><rpm:entry name="gnome-shell"/><rpm:entry name="libgnome-shell.so()(64bit)"/></rpm:provides>
<rpm:requires><rpm:entry name="mutter"/></rpm:requires></format></package>
</metadata>"""


def repomd(primary_name: str, sha: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo"><revision>1725000000</revision>
<data type="primary"><checksum type="sha256">{sha}</checksum><location href="repodata/{primary_name}"/></data>
</repomd>""".encode()


class FakeRegistry:
    """ghcr-shaped: /token, /v2/<path>/manifests/<ref>, /v2/<path>/blobs/<digest>."""

    def __init__(self, layer: bytes, digest: str, *, index: bool = False):
        self.layer, self.digest, self.index = layer, digest, index
        self.hits: list[str] = []

    def __call__(self, request, timeout=0):
        url = request.full_url
        self.hits.append(url)
        if "/token?" in url:
            return io.BytesIO(json.dumps({"token": "anon"}).encode())
        assert request.headers.get("Authorization") == "Bearer anon", "blob/manifest fetched without the pull token"
        if url.endswith(f"/manifests/{self.digest}") and self.index:
            return io.BytesIO(json.dumps({
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {"digest": "sha256:" + "a" * 64, "platform": {"architecture": "arm64", "os": "linux"}},
                    {"digest": "sha256:" + "b" * 64, "platform": {"architecture": "amd64", "os": "linux"}},
                ]}).encode())
        if "/manifests/" in url:
            return io.BytesIO(json.dumps({
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "layers": [{"digest": "sha256:" + "c" * 64, "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}],
            }).encode())
        if "/blobs/" in url:
            return io.BytesIO(self.layer)
        raise AssertionError(f"unexpected fetch {url}")


def make_layer() -> tuple[bytes, str]:
    primary_gz = gzip.compress(PRIMARY_XML)
    import hashlib
    sha = hashlib.sha256(primary_gz).hexdigest()
    name = f"{sha}-primary.xml.gz"
    # an RPM before the repodata, the way createrepo_c writes trees
    layer = oci.make_layer({
        "repository/gnome-shell-51.0-1.hum1.bfin.x86_64.rpm": b"not really an rpm" * 100,
        f"repository/repodata/{name}": primary_gz,
        "repository/repodata/repomd.xml": repomd(name, sha),
    })
    return layer, sha


REF = "oci://ghcr.io/projectbluefin/utah-packages@sha256:" + "9c" * 32


def test_the_reader_walks_token_manifest_and_layer_and_records_the_digest(tmp_path, monkeypatch):
    layer, sha = make_layer()
    registry = FakeRegistry(layer, "sha256:" + "9c" * 32)
    monkeypatch.setattr(oci, "urlopen", registry)
    primary, prov = oci.primary_of(REF, tmp_path / "cache")
    assert primary == PRIMARY_XML
    assert prov["image_digest"] == "sha256:" + "9c" * 32
    assert prov["primary_sha256"] == sha == prov["primary_sha256_declared"]
    assert prov["baseurl"] == REF and prov["revision"] == "1725000000"
    assert any("/token?" in h for h in registry.hits)
    assert any("/blobs/sha256:" + "c" * 64 in h for h in registry.hits)


def test_the_reader_is_cached_by_digest(tmp_path, monkeypatch):
    layer, _ = make_layer()
    registry = FakeRegistry(layer, "sha256:" + "9c" * 32)
    monkeypatch.setattr(oci, "urlopen", registry)
    oci.primary_of(REF, tmp_path / "cache")
    fetched = len(registry.hits)
    again, prov = oci.primary_of(REF, tmp_path / "cache")
    assert again == PRIMARY_XML and prov["image_digest"].endswith("9c" * 32)
    assert len(registry.hits) == fetched, "a pinned digest is fetched once per cache lifetime"


def test_a_multi_arch_index_picks_the_platform_for_the_rpm_arch(tmp_path, monkeypatch):
    layer, _ = make_layer()
    registry = FakeRegistry(layer, "sha256:" + "9c" * 32, index=True)
    monkeypatch.setattr(oci, "urlopen", registry)
    oci.primary_of(REF, tmp_path / "cache", arch="x86_64")
    assert any("/manifests/sha256:" + "b" * 64 in h for h in registry.hits), "x86_64 -> amd64 manifest"
    assert not any("/manifests/sha256:" + "a" * 64 in h for h in registry.hits)


def test_tags_are_refused():
    with pytest.raises(SystemExit, match="digest"):
        oci.parse_ref("oci://ghcr.io/projectbluefin/utah-packages:latest")


def test_gap_engine_dispatches_oci_references_to_the_reader(tmp_path, monkeypatch):
    layer, _ = make_layer()
    monkeypatch.setattr(oci, "urlopen", FakeRegistry(layer, "sha256:" + "9c" * 32))
    # gap_engine loads oci_repository by path; point the loaded copy at the fake too
    monkeypatch.setattr(gap, "_oci", lambda: oci)
    primary, prov = gap.primary_of(REF, tmp_path / "cache")
    assert prov["image_digest"].endswith("9c" * 32)
    index = gap.parse_primary(primary)
    assert "gnome-shell" in index["packages"]


# ── what consuming means to the engine and the checker ─────────────────────


def test_consumed_indexes_fold_into_have(tmp_path, monkeypatch):
    layer, _ = make_layer()
    monkeypatch.setattr(oci, "urlopen", FakeRegistry(layer, "sha256:" + "9c" * 32))
    monkeypatch.setattr(gap, "_oci", lambda: oci)
    measurement = {"consumed_indexes": [{"id": "utah-packages", "index": REF}]}
    entries = gap.consumed_indexes(measurement, "x86_64", tmp_path / "cache")
    assert [e["id"] for e in entries] == ["utah-packages"]
    assert entries[0]["binary_packages"] == 1
    have = entries[0]["_have"]
    assert {"gnome-shell", "libgnome-shell.so()(64bit)"} <= have, (
        "a root utah ships must count as had, or the build order rebuilds GNOME"
    )
    assert gap.consumed_indexes({}, "x86_64", tmp_path) == []
    assert gap.consumed_indexes(None, "x86_64", tmp_path) == []


def test_json_report_drops_private_consumed_index_sets(monkeypatch, capsys):
    empty = {"packages": {}, "provides": {}, "provides_evr": {}, "files": set()}

    def fake_primary_of(url, cache, arch=None):
        return b"", {"baseurl": url, "revision": "test"}

    def fake_consumed_indexes(measurement, arch, cache):
        return [{
            "id": "utah-packages",
            "baseurl": "https://consumed/",
            "binary_packages": 0,
            "_have": set(),
            "_have_evr": {"capability": {"1-1"}},
        }]

    monkeypatch.setattr(chk.gap, "primary_of", fake_primary_of)
    monkeypatch.setattr(chk.gap, "parse_primary", lambda blob: empty)
    monkeypatch.setattr(chk.gap, "consumed_indexes", fake_consumed_indexes)

    assert chk.main([
        "--target-index", "https://target/",
        "--published-index", "https://published/",
        "--consumed-index", "https://consumed/",
        "--desktop", "gnome",
        "--json",
    ]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["consumed_indexes"] == [{
        "baseurl": "https://consumed/",
        "binary_packages": 0,
        "id": "utah-packages",
    }]


def index_of(*packages: tuple[str, list[str], list[str]]) -> dict:
    """A parsed-primary lookalike: name -> (provides, requires)."""
    out = {"packages": {}, "provides": {}, "provides_evr": {}, "files": set()}
    for name, provides, requires in packages:
        out["packages"][name] = {"requires": requires, "provides": [name, *provides]}
        for cap in [name, *provides]:
            out["provides"].setdefault(cap, set()).add(name)
    return out


def test_the_checker_enables_the_consumed_repo_and_attributes_its_needers():
    catalog = {"desktops": {"gnome": {"required_packages": ["gnome-shell"], "install_packages": ["gnome-shell"]}}}
    target = index_of(("glibc", ["libc.so.6()(64bit)"], []))
    published = index_of()
    utah = index_of(("gnome-shell", [], ["mutter", "libc.so.6()(64bit)"]),
                    ("mutter", [], ["libgstplay-1.0.so.0()(64bit)"]))
    # without utah: the root is absent from every repo
    without = chk.check(catalog, target, published, ["gnome"])
    assert without["gnome"]["roots_absent"] == ["gnome-shell"]
    # with utah: the root resolves, and the one thing utah's mutter needs that
    # nobody ships is named and attributed to the CONSUMED side
    with_utah = chk.check(catalog, target, published, ["gnome"], consumed=[utah])
    assert with_utah["gnome"]["roots_absent"] == []
    assert list(with_utah["gnome"]["unresolved"]) == ["libgstplay-1.0.so.0()(64bit)"]
    assert with_utah["gnome"]["unresolved"]["libgstplay-1.0.so.0()(64bit)"]["needer_from"] == ["consumed"]


def test_the_checker_reads_the_contracts_consumed_indexes_by_default():
    text = (ROOT / "scripts" / "check-hummingbird-installability.py").read_text()
    assert 'target.get("gap_measurement")' in text and "consumed_indexes" in text
    assert "--consumed-index" in text
