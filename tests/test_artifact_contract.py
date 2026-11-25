"""Opt-in semantic artifact contracts over real RPM-format and DEB archives (#944)."""
from __future__ import annotations

import importlib.util
import io
import shutil
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("artifact_contract", ROOT / "scripts" / "artifact_contract.py")
ac = importlib.util.module_from_spec(SPEC)
sys.modules["artifact_contract"] = ac
SPEC.loader.exec_module(ac)

S_REG, S_DIR = 0o100000, 0o040000


def _header(tags: dict[int, tuple[int, object]], pad: bool = False) -> bytes:
    store, index = b"", b""
    for tag in sorted(tags):
        kind, value = tags[tag]
        if kind == 6:
            blob, n = value.encode() + b"\0", 1
        elif kind == 8:
            blob, n = b"".join(v.encode() + b"\0" for v in value), len(value)
        elif kind == 4:
            blob, n = b"".join(struct.pack(">I", v) for v in value), len(value)
            store += b"\0" * (-len(store) % 4)
        elif kind == 3:
            blob, n = b"".join(struct.pack(">H", v) for v in value), len(value)
            store += b"\0" * (-len(store) % 2)
        index += struct.pack(">IIII", tag, kind, len(store), n)
        store += blob
    out = b"\x8e\xad\xe8\x01\0\0\0\0" + struct.pack(">II", len(tags), len(store)) + index + store
    if pad:
        out += b"\0" * (-len(out) % 8)
    return out


def make_rpm(path: Path, name: str, files: list, scriptlets: dict | None = None) -> Path:
    """files: (path, mode, flags). Metadata-only RPM: lead + signature + header."""
    dirs: list[str] = []
    base, dir_idx, modes, flags = [], [], [], []
    for full, mode, flag in files:
        d, b = full.rsplit("/", 1)
        d += "/"
        if d not in dirs:
            dirs.append(d)
        base.append(b)
        dir_idx.append(dirs.index(d))
        modes.append(mode)
        flags.append(flag)
    tags = {1000: (6, name)}
    if files:
        tags |= {1117: (8, base), 1118: (8, dirs), 1116: (4, dir_idx), 1030: (3, modes), 1037: (4, flags)}
    for tag, body in (scriptlets or {}).items():
        tags[tag] = (6, body)
    lead = b"\xed\xab\xee\xdb\x03\x00\x00\x00" + b"\0" * 88
    path.write_bytes(lead + _header({1000: (6, "sig")}, pad=True) + _header(tags))
    return path


def make_deb(path: Path, name: str, files: dict, control_extra: dict | None = None, conffiles=()) -> Path:
    root = path.parent / f"{name}-root"
    shutil.rmtree(root, ignore_errors=True)
    (root / "DEBIAN").mkdir(parents=True)
    for rel, (mode, body) in files.items():
        target = root / rel.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        target.chmod(mode)
    (root / "DEBIAN/control").write_text(
        f"Package: {name}\nVersion: 1\nArchitecture: all\nMaintainer: t <t@example.com>\nDescription: t\n"
    )
    for fname, body in (control_extra or {}).items():
        (root / "DEBIAN" / fname).write_text(body)
        (root / "DEBIAN" / fname).chmod(0o755)
    if conffiles:
        (root / "DEBIAN/conffiles").write_text("\n".join(conffiles) + "\n")
    subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "-b", str(root), str(path)], check=True, capture_output=True)
    return path


CONTRACT = {
    "files": [
        {"path": "/usr/bin/tool", "type": "file", "mode": "0755"},
        {"path": "/etc/tool.conf", "type": "file", "mode": "0644", "config": True},
    ],
    "absent": ["/usr/bin/old-tool"],
    "scriptlets": [{"name": "post-install", "contains": "systemctl"}],
}

RPM_OK = [
    ("/usr/bin/tool", S_REG | 0o755, 0),
    ("/etc/tool.conf", S_REG | 0o644, ac.RPMFILE_CONFIG | ac.RPMFILE_NOREPLACE),
]
DEB_OK = {"/usr/bin/tool": (0o755, "x"), "/etc/tool.conf": (0o644, "k=v")}


def good_rpm(tmp_path, **kw):
    return make_rpm(tmp_path / "t.rpm", "tool", kw.get("files", RPM_OK), kw.get("scriptlets", {1024: "systemctl daemon-reload"}))


def good_deb(tmp_path, **kw):
    return make_deb(
        tmp_path / "t.deb", "tool", kw.get("files", DEB_OK),
        kw.get("control", {"postinst": "#!/bin/sh\nsystemctl daemon-reload\n"}),
        kw.get("conffiles", ["/etc/tool.conf"]),
    )


def test_same_contract_holds_for_rpm_and_deb(tmp_path):
    assert ac.check(ac.load_artifact(good_rpm(tmp_path)), CONTRACT) == []
    assert ac.check(ac.load_artifact(good_deb(tmp_path)), CONTRACT) == []


def test_misdeclared_config_mode_and_missing_file_are_precise(tmp_path):
    rpm = make_rpm(tmp_path / "a.rpm", "tool", [("/usr/bin/tool", S_REG | 0o644, 0), ("/etc/tool.conf", S_REG | 0o644, 0)],
                   {1024: "systemctl x"})
    findings = ac.check(ac.load_artifact(rpm), CONTRACT)
    assert any("/usr/bin/tool: mode 0644, contract says 0755" in f for f in findings)
    assert any("/etc/tool.conf" in f and "ordinary file" in f for f in findings)
    deb = good_deb(tmp_path, files={"/usr/bin/tool": (0o755, "x")}, conffiles=[])
    findings = ac.check(ac.load_artifact(deb), CONTRACT)
    assert any("/etc/tool.conf: declared path is missing" in f for f in findings)


def test_rpm_config_without_noreplace_is_a_finding(tmp_path):
    rpm = make_rpm(tmp_path / "a.rpm", "tool", [("/usr/bin/tool", S_REG | 0o755, 0), ("/etc/tool.conf", S_REG | 0o644, ac.RPMFILE_CONFIG)],
                   {1024: "systemctl x"})
    assert any("not noreplace" in f for f in ac.check(ac.load_artifact(rpm), CONTRACT))


def test_missing_scriptlet_and_forbidden_path(tmp_path):
    rpm = make_rpm(tmp_path / "a.rpm", "tool", [*RPM_OK, ("/usr/bin/old-tool", S_REG | 0o755, 0)])
    findings = ac.check(ac.load_artifact(rpm), CONTRACT)
    assert any("post-install scriptlet is missing" in f for f in findings)
    assert any("/usr/bin/old-tool: must not ship" in f for f in findings)
    deb = good_deb(tmp_path, control={"postinst": "#!/bin/sh\ntrue\n"})
    assert any("does not contain 'systemctl'" in f for f in ac.check(ac.load_artifact(deb), CONTRACT))


def test_formats_and_package_scope_entries(tmp_path):
    block = {"files": [{"path": "/etc/only-rpm", "formats": ["rpm"]}, {"path": "/usr/bin/tool", "package": "other"}]}
    assert ac.check(ac.load_artifact(good_deb(tmp_path)), block) == []
    assert ac.check(ac.load_artifact(good_rpm(tmp_path)), block)


@pytest.mark.parametrize("payload", [b"", b"not an archive at all", b"\xed\xab\xee\xdb" + b"\0" * 20, b"!<arch>\n", b"!<arch>\ngarbage" * 8])
def test_malformed_or_unknown_archives_cannot_pass(tmp_path, payload):
    path = tmp_path / "x.bin"
    path.write_bytes(payload)
    with pytest.raises(ac.ContractError):
        ac.load_artifact(path)


def test_truncated_rpm_is_rejected(tmp_path):
    data = good_rpm(tmp_path).read_bytes()
    (tmp_path / "cut.rpm").write_bytes(data[:-10])
    with pytest.raises(ac.ContractError):
        ac.load_artifact(tmp_path / "cut.rpm")


@pytest.mark.parametrize("raw", ["../etc/passwd", "usr/../../x", "a\\b", "/usr/./x", ""])
def test_unsafe_archive_paths_are_rejected(raw):
    with pytest.raises(ac.ContractError):
        ac.safe_path(raw)


def test_rpm_with_traversal_path_is_rejected(tmp_path):
    rpm = make_rpm(tmp_path / "e.rpm", "tool", [("/usr/../etc/x", S_REG | 0o644, 0)])
    with pytest.raises(ac.ContractError):
        ac.load_artifact(rpm)


def test_deb_data_with_traversal_member_is_rejected(tmp_path):
    good = good_deb(tmp_path)
    members = []
    with open(good, "rb") as handle:
        for name, size, h in ac._ar_members(handle):
            members.append((name, h.read(size)))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("../evil")
        info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    out = b"!<arch>\n"
    for name, data in members:
        if name.startswith("data.tar"):
            name, data = "data.tar.gz", buffer.getvalue()
        out += f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n".encode() + data + (b"\n" if len(data) & 1 else b"")
    (tmp_path / "evil.deb").write_bytes(out)
    with pytest.raises(ac.ContractError):
        ac.load_artifact(tmp_path / "evil.deb")


def test_decompression_bomb_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "MAX_TAR_STREAM", 4096)
    deb = make_deb(tmp_path / "big.deb", "tool", {"/usr/share/blob": (0o644, "A" * 200_000)})
    with pytest.raises(ac.ContractError, match="bound"):
        ac.load_artifact(deb)


@pytest.mark.parametrize("block", [
    {}, {"files": [{"path": "usr/bin/x"}]}, {"files": [{"path": "/usr/../x"}]},
    {"files": [{"path": "/x", "mode": 755}]}, {"files": [{"path": "/x", "type": "socket"}]},
    {"files": [{"path": "/x", "bogus": 1}]}, {"scriptlets": [{"name": "postinst"}]},
    {"files": [{"path": "/x", "formats": ["apk"]}]}, {"surprise": []},
])
def test_contract_schema_rejects_defects(block):
    with pytest.raises(ac.ContractError):
        ac.validate_contract(block)


def test_cli_exit_codes_and_opt_in(tmp_path, capsys):
    recipe = tmp_path / "package.yaml"
    recipe.write_text(yaml.safe_dump({"verify": {"smoke": "true", "artifact": CONTRACT}}))
    assert ac.main([str(recipe), str(good_rpm(tmp_path)), str(good_deb(tmp_path))]) == 0
    bad = make_rpm(tmp_path / "bad.rpm", "tool", [])
    assert ac.main([str(recipe), str(bad)]) == 1
    assert "FINDING" in capsys.readouterr().err
    plain = tmp_path / "plain.yaml"
    plain.write_text(yaml.safe_dump({"verify": {"smoke": "true"}}))
    assert ac.main([str(plain), str(good_rpm(tmp_path))]) == 0
    junk = tmp_path / "junk"
    junk.write_bytes(b"zzzz")
    assert ac.main([str(plain), str(junk)]) == 1


def test_tuna_desktop_declares_a_valid_contract():
    recipe = yaml.safe_load((ROOT / "packages/tuna-desktop/package.yaml").read_text())
    block = recipe["verify"]["artifact"]
    ac.validate_contract(block)
    declared = {item["path"].lstrip("/") for item in block["files"]}
    assert declared <= set(recipe["files"]["common"])


def test_tideforge_verify_schema_accepts_and_rejects_artifact_blocks():
    spec = importlib.util.spec_from_file_location("tideforge", ROOT / "scripts" / "tideforge.py")
    tf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tf)
    base = {"name": "x", "targets": ["el10"], "verify": {"smoke": "true", "artifact": CONTRACT}}
    tf.validate_verify(base)
    base["verify"] = {"smoke": "true", "artifact": {"files": [{"path": "bad"}]}}
    with pytest.raises(SystemExit):
        tf.validate_verify(base)
