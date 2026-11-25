"""Re-publishing an unchanged deb package set preserves signed metadata (#943).

Real gpg, throwaway keys; skipped where gpg is absent.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "deb_index_sign", ROOT / "scripts" / "deb-index-sign.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None, reason="needs gpg")

NOW = dt.datetime(2026, 11, 25, 12, 0, tzinfo=dt.timezone.utc)


def release(date="Wed, 25 Nov 2026 10:00:00 UTC", sha="a" * 64, extra=""):
    return (f"Date: {date}\n{extra}SHA256:\n {sha}  100 Packages\n")


def make_key(home: pathlib.Path) -> pathlib.Path:
    home.mkdir(mode=0o700)
    subprocess.run(["gpg", "--batch", "--homedir", str(home),
                    "--passphrase", "", "--quick-gen-key", "t@example.org",
                    "default", "default", "never"],
                   check=True, capture_output=True)
    pub = home / "pub.gpg"
    subprocess.run(["gpg", "--batch", "--homedir", str(home), "--armor",
                    "--export", "-o", str(pub)], check=True)
    return pub


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "gnupg"
    pub = make_key(home)
    repo = tmp_path / "repo"
    repo.mkdir()
    return home, pub, repo, tmp_path


def publish(env, text, now=NOW, pub=None, home=None):
    h, p, repo, tmp = env
    cand = tmp / "Release.new"
    cand.write_text(text)
    return mod.run(repo, cand, pub or p, "t@example.org", now=now,
                   signing_home=home or h)


def snapshot(repo):
    return {n: (repo / n).read_bytes()
            for n in ("Release", "InRelease", "Release.gpg")}


def test_first_publish_signs(env):
    assert publish(env, release())[0] == "renewed"
    assert set(snapshot(env[2])) == {"Release", "InRelease", "Release.gpg"}


def test_unchanged_set_preserves_bytes_despite_new_date(env):
    publish(env, release())
    before = snapshot(env[2])
    later = NOW + dt.timedelta(hours=3)
    out, _ = publish(env, release(date="Wed, 25 Nov 2026 13:00:00 UTC"), now=later)
    assert out == "preserved"
    assert snapshot(env[2]) == before


def test_changed_index_renews_and_verifies(env):
    publish(env, release())
    out, why = publish(env, release(sha="b" * 64))
    assert (out, why) == ("renewed", "package indexes changed")
    assert b"b" * 64 in (env[2] / "Release").read_bytes()


def test_stale_release_renews(env):
    publish(env, release())
    out, why = publish(env, release(), now=NOW + dt.timedelta(days=8))
    assert out == "renewed" and "freshness" in why


def test_expired_valid_until_renews(env):
    t = release(extra="Valid-Until: Wed, 25 Nov 2026 11:00:00 UTC\n")
    publish(env, t)
    out, why = publish(env, t, now=NOW)
    assert out == "renewed" and "expired" in why


def test_tampered_release_is_not_preserved(env):
    publish(env, release())
    (env[2] / "Release").write_text(release(sha="c" * 64))
    out, why = publish(env, release(sha="c" * 64))
    assert out == "renewed" and "unusable" in why


def test_inrelease_release_gpg_payload_mismatch_rejected(env):
    h, p, repo, tmp = env
    publish(env, release())
    other = tmp / "other"
    other.write_text(release(sha="d" * 64))
    subprocess.run(["gpg", "--batch", "--homedir", str(h), "-u", "t@example.org",
                    "-abs", "--yes", "-o", str(repo / "Release.gpg"), str(other)],
                   check=True, capture_output=True)
    (tmp / "v").mkdir(mode=0o700)
    trusted = mod.trusted_fingerprints(tmp / "v", p)
    with pytest.raises(mod.SignatureError):
        mod.verify_signatures(tmp / "v", trusted, repo / "Release",
                              repo / "InRelease", repo / "Release.gpg")


def test_key_rotation_renews_with_new_key(env, tmp_path):
    publish(env, release())
    new_home = tmp_path / "gnupg2"
    new_pub = make_key(new_home)
    out, why = publish(env, release(), pub=new_pub, home=new_home)
    assert out == "renewed" and "unusable" in why
