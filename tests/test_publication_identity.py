"""Publication identity: format-qualified, digest-conflict aware (#942)."""
from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "publication_identity", ROOT / "scripts" / "publication_identity.py")
pi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pi)


def row(name="foo", evr="1.0-1", arch="x86_64"):
    return {"name": name, "evr": evr, "arch": arch}


def test_equal_names_across_format_target_arch_do_not_collide():
    idents = [
        pi.identity("rpm", "el10", row()),
        pi.identity("deb", "ubuntu", row()),
        pi.identity("rpm", "el10", row(arch="aarch64")),
        pi.identity("pkg.tar.zst", "arch", row()),
        pi.identity("gpkg", "gentoo-glibc", row()),
        pi.identity("gpkg", "gentoo-musl", row()),
    ]
    assert len(set(idents)) == len(idents)
    conflicts = pi.detect_digest_conflicts(
        (ident, f"sha256:{n}") for n, ident in enumerate(idents))
    assert conflicts == []


def test_source_and_binary_are_distinct_identities():
    assert pi.identity("rpm", "el10", row(arch="src")).kind == "source"
    assert pi.identity("rpm", "el10", row()).kind == "binary"
    assert (pi.identity("rpm", "el10", row(arch="src"))
            != pi.identity("rpm", "el10", row(arch="x86_64")))


def test_exact_republication_is_idempotent():
    ident = pi.identity("rpm", "el10", row())
    assert pi.admit({ident: "sha256:a"}, [(ident, "sha256:a")]) == []


def test_changed_bytes_under_a_published_identity_are_rejected():
    ident = pi.identity("deb", "ubuntu", row())
    found = pi.admit({ident: "sha256:a"}, [(ident, "sha256:b")])
    assert len(found) == 1
    assert found[0]["digests"] == ["sha256:a", "sha256:b"]


def test_version_bump_is_not_a_conflict():
    old = pi.identity("rpm", "el10", row(evr="1.0-1"))
    new = pi.identity("rpm", "el10", row(evr="1.0-2"))
    assert pi.admit({old: "sha256:a"}, [(new, "sha256:b")]) == []


def test_bad_kind_is_refused():
    try:
        pi.identity("rpm", "el10", row(), kind="bogus")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
