#!/usr/bin/env python3
"""Semantic contracts for built RPM and DEB artifacts.

A recipe may opt in with `verify.artifact`, declaring what the consumer sees
in the *built archive*: which paths ship, their type and permission bits,
which are configuration files that survive an upgrade, which must not ship,
and which maintainer scriptlets must exist. The same declaration is checked
against an RPM and a DEB through one format-independent model.

This complements, and does not replace, the install smoke (`verify.smoke`),
lintian/rpmlint and scriptlet generation: it reads archive metadata only.
Metadata cannot prove installed ownership or upgrade behaviour; the native
install/upgrade gates remain authoritative for that.

Extraction is bounded and never writes to disk: RPM metadata is parsed from
the header sections alone, DEB members are streamed through a byte-counting
reader, and every archive path is validated before it enters the model. An
unknown or malformed archive is an error, never a pass.
"""
from __future__ import annotations

import argparse
import bz2
import contextlib
import gzip
import io
import lzma
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import tarfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

MAX_ENTRIES = 200_000
MAX_HEADER_INDEX = 200_000
MAX_HEADER_DATA = 64 * 1024 * 1024
MAX_TAR_STREAM = 1024 * 1024 * 1024  # decompressed bytes read from one tar
MAX_CONTROL_MEMBER = 4 * 1024 * 1024
MAX_AR_MEMBER = 4 * 1024 * 1024 * 1024
MAX_SCRIPTLET = 1024 * 1024
MAX_ZSTD_MEMBER = 512 * 1024 * 1024

SCRIPTLETS = ("pre-install", "post-install", "pre-remove", "post-remove")
RPM_SCRIPTLET_TAGS = {
    "pre-install": (1023, 1085),
    "post-install": (1024, 1086),
    "pre-remove": (1025, 1087),
    "post-remove": (1026, 1088),
}
DEB_SCRIPTLET_FILES = {
    "pre-install": "preinst",
    "post-install": "postinst",
    "pre-remove": "prerm",
    "post-remove": "postrm",
}
FORMATS = ("rpm", "deb")
KINDS = ("file", "dir", "symlink")
RPMFILE_CONFIG = 1 << 0
RPMFILE_NOREPLACE = 1 << 4
RPMFILE_GHOST = 1 << 6


class ContractError(Exception):
    """The artifact or contract is malformed. Never downgraded to a pass."""


@dataclass
class Entry:
    kind: str  # file | dir | symlink | other
    mode: int  # permission bits (incl. setuid/setgid/sticky)
    config: bool = False
    noreplace: bool = False
    ghost: bool = False


@dataclass
class Model:
    format: str
    name: str
    files: dict[str, Entry] = field(default_factory=dict)
    scriptlets: dict[str, str] = field(default_factory=dict)


def safe_path(raw: str) -> str:
    """Normalise an archive path to `/abs/path`; reject anything ambiguous."""
    if not raw or "\x00" in raw or "\\" in raw:
        raise ContractError(f"unsafe archive path {raw!r}")
    text = raw[2:] if raw.startswith("./") else raw
    parts = text.strip("/").split("/")
    if not text.strip("/") or any(part in ("", ".", "..") for part in parts):
        raise ContractError(f"unsafe archive path {raw!r}")
    return "/" + "/".join(parts)


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


# ---------------------------------------------------------------- RPM

RPM_MAGIC = b"\xed\xab\xee\xdb"
HEADER_MAGIC = b"\x8e\xad\xe8\x01"


def _read_exact(handle, size: int, what: str) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise ContractError(f"truncated RPM: {what}")
    return data


def _rpm_header(handle, what: str, pad8: bool) -> dict[int, tuple[int, int, bytes, bytes]]:
    magic = _read_exact(handle, 16, f"{what} header")
    if magic[:4] != HEADER_MAGIC:
        raise ContractError(f"malformed RPM: bad {what} header magic")
    count, size = struct.unpack(">II", magic[8:16])
    if count > MAX_HEADER_INDEX or size > MAX_HEADER_DATA:
        raise ContractError(f"RPM {what} header exceeds bounds ({count} entries, {size} bytes)")
    index = _read_exact(handle, 16 * count, f"{what} index")
    store = _read_exact(handle, size, f"{what} data")
    if pad8:
        _read_exact(handle, -(16 + 16 * count + size) % 8, f"{what} padding")
    tags = {}
    for i in range(count):
        tag, kind, offset, n = struct.unpack(">IIII", index[16 * i : 16 * i + 16])
        if offset > size:
            raise ContractError(f"malformed RPM: tag {tag} offset outside data")
        tags[tag] = (kind, n, store[offset:], store)
    return tags


def _strings(entry, count_hint=None) -> list[str]:
    kind, n, blob, _ = entry
    if kind not in (6, 8, 9):
        raise ContractError("malformed RPM: expected string tag")
    out, pos = [], 0
    for _ in range(n):
        end = blob.find(b"\x00", pos)
        if end < 0:
            raise ContractError("malformed RPM: unterminated string")
        out.append(blob[pos:end].decode("utf-8", "surrogateescape"))
        pos = end + 1
    return out


def _ints(entry) -> list[int]:
    kind, n, blob, _ = entry
    width = {3: 2, 4: 4, 5: 8}.get(kind)
    if width is None or len(blob) < width * n:
        raise ContractError("malformed RPM: bad integer tag")
    fmt = {2: ">H", 4: ">I", 8: ">Q"}[width]
    return [struct.unpack_from(fmt, blob, width * i)[0] for i in range(n)]


def parse_rpm(handle) -> Model:
    lead = _read_exact(handle, 96, "lead")
    if lead[:4] != RPM_MAGIC:
        raise ContractError("not an RPM")
    sig = _rpm_header(handle, "signature", pad8=True)  # noqa: F841 - consumed to reach the header
    tags = _rpm_header(handle, "metadata", pad8=False)
    name = _strings(tags[1000])[0] if 1000 in tags else ""
    if not name:
        raise ContractError("malformed RPM: no Name tag")
    model = Model("rpm", name)

    if 1117 in tags:  # BASENAMES
        base = _strings(tags[1117])
        dirs = _strings(tags[1118])
        dir_index = _ints(tags[1116])
        modes = _ints(tags[1030])
        flags = _ints(tags[1037]) if 1037 in tags else [0] * len(base)
        if not (len(base) == len(dir_index) == len(modes) == len(flags)):
            raise ContractError("malformed RPM: file tag arrays disagree in length")
        if len(base) > MAX_ENTRIES:
            raise ContractError("RPM lists too many files")
        paths = []
        for b, di in zip(base, dir_index):
            if di >= len(dirs):
                raise ContractError("malformed RPM: directory index out of range")
            paths.append(dirs[di] + b)
    elif 1027 in tags:  # OLDFILENAMES
        paths = _strings(tags[1027])
        modes = _ints(tags[1030])
        flags = _ints(tags[1037]) if 1037 in tags else [0] * len(paths)
        if not (len(paths) == len(modes) == len(flags)):
            raise ContractError("malformed RPM: file tag arrays disagree in length")
    else:
        paths, modes, flags = [], [], []
    for path, mode, flag in zip(paths, modes, flags):
        key = safe_path(path)
        model.files[key] = Entry(
            _kind(mode), mode & 0o7777,
            config=bool(flag & RPMFILE_CONFIG),
            noreplace=bool(flag & RPMFILE_NOREPLACE),
            ghost=bool(flag & RPMFILE_GHOST),
        )
    for scriptlet, (body_tag, prog_tag) in RPM_SCRIPTLET_TAGS.items():
        body = _strings(tags[body_tag])[0] if body_tag in tags else ""
        if not body and prog_tag in tags:
            prog = tags[prog_tag]
            body = " ".join(_strings(prog)) if prog[0] in (6, 8, 9) else ""
            body = f"<interpreter {body}>" if body else ""
        if body:
            if len(body) > MAX_SCRIPTLET:
                raise ContractError(f"RPM {scriptlet} scriptlet exceeds bounds")
            model.scriptlets[scriptlet] = body
    return model


# ---------------------------------------------------------------- DEB


class _Bounded(io.RawIOBase):
    """File-like wrapper that refuses to yield more than `limit` bytes."""

    def __init__(self, source, limit: int, what: str):
        self.source, self.limit, self.what, self.used = source, limit, what, 0

    def readable(self):
        return True

    def readinto(self, buffer):
        data = self.source.read(len(buffer))
        self.used += len(data)
        if self.used > self.limit:
            raise ContractError(f"{self.what} exceeds the {self.limit}-byte extraction bound")
        buffer[: len(data)] = data
        return len(data)


def _ar_members(handle):
    if _read_exact(handle, 8, "ar magic") != b"!<arch>\n":
        raise ContractError("not a DEB (missing ar magic)")
    while True:
        header = handle.read(60)
        if not header:
            return
        if len(header) != 60 or header[58:60] != b"`\n":
            raise ContractError("malformed DEB: bad ar member header")
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode().strip())
        except ValueError:
            raise ContractError("malformed DEB: bad ar member size") from None
        if size > MAX_AR_MEMBER:
            raise ContractError(f"DEB member {name} exceeds bounds")
        start = handle.tell()
        yield name, size, handle
        handle.seek(start + size + (size & 1))


@contextlib.contextmanager
def _tar_stream(handle, size: int, name: str):
    """A streaming TarFile over one ar member; decompressed bytes are capped."""
    window = _Window(handle, size)
    cleanup = []
    try:
        if name.endswith(".tar"):
            source = window
        elif name.endswith(".gz"):
            source = gzip.GzipFile(fileobj=io.BufferedReader(window))
        elif name.endswith(".xz"):
            source = lzma.LZMAFile(io.BufferedReader(window))
        elif name.endswith(".bz2"):
            source = bz2.BZ2File(io.BufferedReader(window))
        elif name.endswith(".zst"):
            zstd = shutil.which("zstd")
            if not zstd:
                raise ContractError(f"{name}: zstd member but no zstd binary to read it (failing closed)")
            if size > MAX_ZSTD_MEMBER:
                raise ContractError(f"{name} exceeds the extraction bound")
            spool = tempfile.TemporaryFile()
            cleanup.append(spool.close)
            shutil.copyfileobj(io.BufferedReader(window), spool)
            spool.seek(0)
            proc = subprocess.Popen([zstd, "-dc", "--no-progress"], stdin=spool, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            cleanup.append(proc.wait)
            cleanup.append(proc.stdout.close)
            cleanup.append(proc.kill)
            source = proc.stdout
        else:
            raise ContractError(f"{name}: unknown compression")
        bounded = io.BufferedReader(_Bounded(source, MAX_TAR_STREAM, name))
        try:
            with tarfile.open(fileobj=bounded, mode="r|") as tar:
                yield tar
        except (OSError, EOFError, lzma.LZMAError, zlib.error) as error:
            raise ContractError(f"{name}: cannot decompress: {error}") from None
    finally:
        for step in reversed(cleanup):
            try:
                step()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass


class _Window(io.RawIOBase):
    """Read-only view of the next `size` bytes of `handle`."""

    def __init__(self, handle, size):
        self.handle, self.left = handle, size

    def readable(self):
        return True

    def readinto(self, buffer):
        want = min(len(buffer), self.left)
        data = self.handle.read(want)
        buffer[: len(data)] = data
        self.left -= len(data)
        return len(data)


def parse_deb(handle) -> Model:
    control: dict[str, str] = {}
    files: dict[str, Entry] = {}
    seen = set()
    version_ok = False
    for name, size, h in _ar_members(handle):
        base = name.split(".")[0]
        if name == "debian-binary":
            if h.read(size).strip() != b"2.0":
                raise ContractError("unsupported DEB format version")
            version_ok = True
        elif base == "control" and name.startswith("control.tar"):
            seen.add("control")
            try:
                with _tar_stream(h, size, name) as tar:
                    count = 0
                    for member in tar:
                        count += 1
                        if count > 1000:
                            raise ContractError("DEB control archive has too many members")
                        key = member.name[2:] if member.name.startswith("./") else member.name
                        if key in ("", "."):
                            continue
                        if "/" in key or ".." in key:
                            raise ContractError(f"unsafe control member {member.name!r}")
                        if not member.isfile():
                            continue
                        if member.size > MAX_CONTROL_MEMBER:
                            raise ContractError(f"control member {key} exceeds bounds")
                        control[key] = tar.extractfile(member).read().decode("utf-8", "replace")
            except tarfile.TarError as error:
                raise ContractError(f"malformed DEB control archive: {error}") from None
        elif base == "data" and name.startswith("data.tar"):
            seen.add("data")
            try:
                with _tar_stream(h, size, name) as tar:
                    for member in tar:
                        if len(files) >= MAX_ENTRIES:
                            raise ContractError("DEB data archive has too many entries")
                        if member.name in (".", "./"):
                            continue
                        key = safe_path(member.name)
                        kind = "dir" if member.isdir() else "symlink" if member.issym() else "file" if member.isreg() or member.islnk() else "other"
                        files[key] = Entry(kind, member.mode & 0o7777)
            except tarfile.TarError as error:
                raise ContractError(f"malformed DEB data archive: {error}") from None
    if not version_ok or seen != {"control", "data"}:
        raise ContractError("malformed DEB: needs debian-binary, control.tar.* and data.tar.*")
    match = re.search(r"^Package:\s*(\S+)\s*$", control.get("control", ""), re.M)
    if not match:
        raise ContractError("malformed DEB: control has no Package field")
    model = Model("deb", match.group(1), files)
    conffiles = {safe_path(line.strip()) for line in control.get("conffiles", "").splitlines() if line.strip()}
    for path in conffiles:
        if path in model.files:
            model.files[path].config = True
            model.files[path].noreplace = True  # dpkg conffiles always keep local edits
    for scriptlet, filename in DEB_SCRIPTLET_FILES.items():
        if filename in control and control[filename].strip():
            if len(control[filename]) > MAX_SCRIPTLET:
                raise ContractError(f"DEB {filename} exceeds bounds")
            model.scriptlets[scriptlet] = control[filename]
    return model


def load_artifact(path: Path) -> Model:
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
            handle.seek(0)
            if head[:4] == RPM_MAGIC:
                return parse_rpm(handle)
            if head == b"!<arch>\n":
                return parse_deb(handle)
    except (OSError, struct.error, EOFError) as error:
        raise ContractError(f"{path}: cannot read artifact: {error}") from None
    raise ContractError(f"{path}: unknown archive format (not an RPM or DEB)")


# ----------------------------------------------------------- contract


def validate_contract(block) -> None:
    """Schema-check `verify.artifact`; raises ContractError."""
    if not isinstance(block, dict):
        raise ContractError("verify.artifact must be a mapping")
    unknown = set(block) - {"files", "absent", "scriptlets"}
    if unknown:
        raise ContractError(f"unknown verify.artifact key(s): {sorted(unknown)}")
    if not any(block.get(key) for key in ("files", "absent", "scriptlets")):
        raise ContractError("verify.artifact declares nothing: give files, absent or scriptlets")

    def scope(item, where):
        for key in ("formats",):
            if key in item and (not isinstance(item[key], list) or not item[key] or not set(item[key]) <= set(FORMATS)):
                raise ContractError(f"{where}.formats must be a non-empty list drawn from {list(FORMATS)}")
        if "package" in item and (not isinstance(item["package"], str) or not item["package"]):
            raise ContractError(f"{where}.package must be a string")

    for i, item in enumerate(block.get("files", [])):
        where = f"verify.artifact.files[{i}]"
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ContractError(f"{where} must be a mapping with a path")
        extra = set(item) - {"path", "type", "mode", "config", "formats", "package"}
        if extra:
            raise ContractError(f"unknown {where} key(s): {sorted(extra)}")
        try:
            safe = safe_path(item["path"])
        except ContractError:
            raise ContractError(f"{where}.path {item['path']!r} is not a clean absolute path") from None
        if not item["path"].startswith("/") or safe != item["path"]:
            raise ContractError(f"{where}.path {item['path']!r} must be a normalised absolute path")
        if "type" in item and item["type"] not in KINDS:
            raise ContractError(f"{where}.type must be one of {list(KINDS)}")
        if "mode" in item and not (isinstance(item["mode"], str) and re.fullmatch(r"[0-7]{4}", item["mode"])):
            raise ContractError(f"{where}.mode must be a quoted four-digit octal string such as \"0644\"")
        if "config" in item and not isinstance(item["config"], bool):
            raise ContractError(f"{where}.config must be true or false")
        scope(item, where)
    for i, item in enumerate(block.get("absent", [])):
        where = f"verify.artifact.absent[{i}]"
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].startswith("/"):
            raise ContractError(f"{where} must be an absolute path")
        if set(item) - {"path", "formats", "package"}:
            raise ContractError(f"unknown {where} key(s)")
        scope(item, where)
    for i, item in enumerate(block.get("scriptlets", [])):
        where = f"verify.artifact.scriptlets[{i}]"
        if not isinstance(item, dict) or item.get("name") not in SCRIPTLETS:
            raise ContractError(f"{where}.name must be one of {list(SCRIPTLETS)}")
        if set(item) - {"name", "contains", "formats", "package"}:
            raise ContractError(f"unknown {where} key(s)")
        if "contains" in item and (not isinstance(item["contains"], str) or not item["contains"]):
            raise ContractError(f"{where}.contains must be a non-empty string")
        scope(item, where)


def _applies(item: dict, model: Model) -> bool:
    if "formats" in item and model.format not in item["formats"]:
        return False
    return "package" not in item or item["package"] == model.name


def check(model: Model, block: dict) -> list[str]:
    """Findings for one artifact; an empty list means the contract holds."""
    findings = []
    label = f"{model.format} {model.name}"
    for item in block.get("files", []):
        if not _applies(item, model):
            continue
        path = item["path"]
        entry = model.files.get(path)
        if entry is None:
            findings.append(f"{label}: {path}: declared path is missing from the artifact")
            continue
        if "type" in item and entry.kind != item["type"]:
            findings.append(f"{label}: {path}: is a {entry.kind}, contract says {item['type']}")
        if "mode" in item and f"{entry.mode:04o}" != item["mode"]:
            findings.append(f"{label}: {path}: mode {entry.mode:04o}, contract says {item['mode']}")
        if item.get("config") is True:
            if not entry.config:
                findings.append(f"{label}: {path}: contract says configuration file, artifact ships it as an ordinary file")
            elif model.format == "rpm" and not entry.noreplace:
                findings.append(f"{label}: {path}: marked %config but not noreplace; an upgrade would overwrite local edits to .rpmsave")
        elif item.get("config") is False and entry.config:
            findings.append(f"{label}: {path}: contract says ordinary file, artifact marks it as configuration")
    for item in block.get("absent", []):
        item = {"path": item} if isinstance(item, str) else item
        if _applies(item, model) and item["path"] in model.files:
            findings.append(f"{label}: {item['path']}: must not ship but is present in the artifact")
    for item in block.get("scriptlets", []):
        if not _applies(item, model):
            continue
        body = model.scriptlets.get(item["name"])
        if body is None:
            findings.append(f"{label}: required {item['name']} scriptlet is missing")
        elif "contains" in item and item["contains"] not in body:
            findings.append(f"{label}: {item['name']} scriptlet does not contain {item['contains']!r}")
    return findings


def main(argv=None) -> int:
    import yaml

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("recipe", type=Path)
    parser.add_argument("artifacts", type=Path, nargs="+")
    args = parser.parse_args(argv)
    recipe = yaml.safe_load(args.recipe.read_text()) or {}
    block = (recipe.get("verify") or {}).get("artifact")
    models = []
    try:
        if block is not None:
            validate_contract(block)
        for path in args.artifacts:
            models.append((path, load_artifact(path)))
    except ContractError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if block is None:
        print(f"{args.recipe}: no verify.artifact contract (opt-in); archives parsed, nothing asserted")
        return 0
    findings = []
    for path, model in models:
        found = check(model, block)
        findings += [f"{path.name}: {line}" for line in found]
    named = {model.name for _, model in models}
    for item in [*block.get("files", []), *block.get("scriptlets", [])]:
        if "package" in item and item["package"] not in named:
            findings.append(f"contract names package {item['package']!r}, which no artifact supplied")
    if findings:
        print("\n".join(f"FINDING: {line}" for line in sorted(set(findings))), file=sys.stderr)
        return 1
    print(f"artifact contract holds for {len(models)} artifact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
