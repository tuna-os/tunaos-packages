#!/usr/bin/env python3
"""Read an rpm-md repository that ships inside an OCI image, without pulling it.

projectbluefin/utah-packages publishes its GNOME 51 for Hummingbird as
`FROM scratch; COPY repository /repository` -- a createrepo_c tree in one
layer, addressed by digest, cosign-signed. tunaOS's hummingbird:gnome consumes
it that way (bind-mounted, by digest). For this factory it is a repository
the target ALREADY HAS: whatever utah ships is not a gap to build, and the
installability walk must count it as enabled. Both need utah's primary.xml.

There is no HTTP baseurl to read it from: the OCI layer IS the repository.
So this walks the registry directly -- anonymous pull token, manifest,
layer blob -- and streams the layer tar through `tarfile`, keeping only
`repository/repodata/*` (repomd.xml plus the primary index it names, a few
MB out of ~500). Nothing is written except the cache entry, keyed by the
image digest, so a pinned digest is fetched once per cache lifetime.

Reference form, mirroring how a Containerfile names it:

    oci://ghcr.io/projectbluefin/utah-packages@sha256:<64 hex>

Only digest references are accepted: a tag is a moving target, and the whole
point of the pin is that a measurement names the bytes it measured.

`urlopen` is a module attribute so tests can stand up a fake registry.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import lzma
import pathlib
import re
import tarfile
import urllib.request
import xml.etree.ElementTree as ET

REF = re.compile(
    r"^oci://(?P<host>[^/]+)/(?P<path>[^@:]+)@(?P<digest>sha256:[0-9a-f]{64})$"
)
ACCEPT = ", ".join((
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
))
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
# rpm arch -> OCI platform architecture, for multi-arch indexes.
PLATFORM = {"x86_64": "amd64", "aarch64": "arm64"}
REPO_NS = "{http://linux.duke.edu/metadata/repo}"
REPODATA = "repository/repodata/"
USER_AGENT = "tunaos-package-factory"

urlopen = urllib.request.urlopen


def parse_ref(ref: str) -> tuple[str, str, str]:
    match = REF.match(ref)
    if not match:
        raise SystemExit(
            f"{ref}: not an oci://host/path@sha256:... reference (tags are not "
            "accepted: a measurement must name the digest it measured)"
        )
    return match["host"], match["path"], match["digest"]


def _get(url: str, headers: dict, timeout: int = 300):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    return urlopen(request, timeout=timeout)


def pull_token(host: str, path: str) -> str | None:
    """Anonymous pull token (ghcr.io and every Docker-registry-v2 host that
    supports the token flow); None when the registry serves blobs without one."""
    try:
        with _get(f"https://{host}/token?scope=repository:{path}:pull", {}, timeout=60) as r:
            return json.load(r).get("token")
    except Exception:  # noqa: BLE001 -- unauthenticated reads are a valid fallback
        return None


def _auth(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def manifest_for(host: str, path: str, digest: str, arch: str, token: str | None) -> dict:
    url = f"https://{host}/v2/{path}/manifests/{digest}"
    with _get(url, {"Accept": ACCEPT, **_auth(token)}) as r:
        doc = json.load(r)
    if doc.get("mediaType") in INDEX_TYPES or "manifests" in doc:
        wanted = PLATFORM.get(arch, arch)
        chosen = None
        for entry in doc.get("manifests", []):
            platform = entry.get("platform") or {}
            if platform.get("architecture") == wanted:
                chosen = entry
                break
        if chosen is None:
            raise SystemExit(f"{host}/{path}@{digest}: no {wanted} manifest in the index")
        with _get(f"https://{host}/v2/{path}/manifests/{chosen['digest']}",
                  {"Accept": ACCEPT, **_auth(token)}) as r:
            doc = json.load(r)
    if "layers" not in doc:
        raise SystemExit(f"{host}/{path}@{digest}: manifest has no layers")
    return doc


def repodata_from_layers(host: str, path: str, manifest: dict, token: str | None) -> dict[str, bytes]:
    """Stream every layer and keep only repository/repodata/*.

    Stops as soon as repomd.xml AND the primary index it names have both been
    seen, so on the common layout (repodata written last by createrepo_c)
    the whole layer is still read, but nothing is buffered beyond the index.
    """
    found: dict[str, bytes] = {}
    primary_href: str | None = None
    for layer in manifest["layers"]:
        url = f"https://{host}/v2/{path}/blobs/{layer['digest']}"
        with _get(url, _auth(token)) as response:
            with tarfile.open(fileobj=response, mode="r|*") as tar:
                for member in tar:
                    name = member.name.lstrip("./")
                    if not name.startswith(REPODATA) or not member.isfile():
                        continue
                    data = tar.extractfile(member).read()
                    found[name[len(REPODATA):]] = data
                    if name.endswith("/repomd.xml"):
                        primary_href = _primary_href(data)
                    if primary_href and primary_href in found and "repomd.xml" in found:
                        return found
        if primary_href and primary_href in found:
            return found
    return found


def _primary_href(repomd: bytes) -> str:
    root = ET.fromstring(repomd)
    for data in root.findall(f"{REPO_NS}data"):
        if data.get("type") != "primary":
            continue
        href = data.find(f"{REPO_NS}location").get("href")
        if not href.endswith(".zck"):
            return href.split("repodata/", 1)[-1]
    raise SystemExit("repomd.xml inside the image names no readable primary index")


def decompress(name: str, data: bytes) -> bytes:
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".zst"):
        import zstandard

        return zstandard.ZstdDecompressor().decompressobj().decompress(data)
    return data


def primary_of(ref: str, cache: pathlib.Path, arch: str = "x86_64") -> tuple[bytes, dict]:
    """Decompressed primary.xml of the repository inside `ref`, plus provenance.

    Same shape as gap_engine.primary_of so the two are interchangeable for
    every consumer: (bytes, {baseurl, revision, primary_href, primary_sha256,
    primary_sha256_declared, primary_bytes}).
    """
    host, path, digest = parse_ref(ref)
    key = hashlib.sha256(f"{ref}|{arch}".encode()).hexdigest()[:16]
    cached = cache / f"{key}.primary.xml"
    meta = cache / f"{key}.json"
    if cached.exists() and meta.exists():
        return cached.read_bytes(), json.loads(meta.read_text())

    token = pull_token(host, path)
    manifest = manifest_for(host, path, digest, arch, token)
    files = repodata_from_layers(host, path, manifest, token)
    if "repomd.xml" not in files:
        raise SystemExit(f"{ref}: no repository/repodata/repomd.xml in any layer")
    href = _primary_href(files["repomd.xml"])
    if href not in files:
        raise SystemExit(f"{ref}: repomd.xml names {href} but the layer does not carry it")
    raw = files[href]
    root = ET.fromstring(files["repomd.xml"])
    declared = None
    for data in root.findall(f"{REPO_NS}data"):
        if data.get("type") == "primary":
            declared = data.findtext(f"{REPO_NS}checksum")
    provenance = {
        "baseurl": ref,
        "revision": root.findtext(f"{REPO_NS}revision"),
        "primary_href": f"repodata/{href}",
        "primary_sha256": hashlib.sha256(raw).hexdigest(),
        "primary_sha256_declared": declared,
        "primary_bytes": len(raw),
        "image_digest": digest,
        "layers": [layer["digest"] for layer in manifest["layers"]],
    }
    primary = decompress(href, raw)
    cache.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(primary)
    meta.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    return primary, provenance


def extract_tree(host: str, path: str, manifest: dict, token: str | None,
                 dest: pathlib.Path, prefix: str = "repository/") -> dict:
    """Stream every layer and write members under `prefix` into `dest`.

    The buildroot twin of repodata_from_layers: instead of keeping only the
    index in memory it materialises the whole repository tree (RPMs
    included) so a mock/dnf buildroot can consume it as a file:// repo.
    Members outside `prefix`, absolute paths, and `..` escapes are skipped
    and counted, never written. Returns {files, skipped, layers}.
    """
    dest.mkdir(parents=True, exist_ok=True)
    files = 0
    skipped: list[str] = []
    for layer in manifest["layers"]:
        url = f"https://{host}/v2/{path}/blobs/{layer['digest']}"
        with _get(url, _auth(token)) as response:
            with tarfile.open(fileobj=response, mode="r|*") as tar:
                for member in tar:
                    if member.name.startswith("/"):
                        skipped.append(member.name)
                        continue
                    name = member.name
                    while name.startswith("./"):
                        name = name[2:]
                    if not name.startswith(prefix):
                        continue
                    rel = name[len(prefix):]
                    if not rel or rel.endswith("/"):
                        continue
                    target = dest / rel
                    try:
                        target.resolve().relative_to(dest.resolve())
                    except ValueError:
                        skipped.append(name)
                        continue
                    if not member.isfile():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as src, open(target, "wb") as out:
                        out.write(src.read())
                    files += 1
    return {"files": files, "skipped": skipped,
            "layers": [layer["digest"] for layer in manifest["layers"]]}


def make_layer(files: dict[str, bytes]) -> bytes:
    """A gzip'd tar with the given member names -> bytes. Test helper, and
    the exact shape `podman build` produces for `COPY repository /repository`."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
