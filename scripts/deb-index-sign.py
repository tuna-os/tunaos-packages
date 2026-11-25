#!/usr/bin/env python3
"""Idempotent Release/InRelease/Release.gpg step for the flat deb repos.

`apt-ftparchive release` stamps a fresh `Date:` on every run, so re-publishing
an unchanged package set used to rewrite Release and both signatures (and so
InRelease/Release.gpg bytes, since gpg signatures are not deterministic) for
no reason. This step makes that an explicit policy (#943), after the contract
frostyard/repogen enforces: both signature forms must cover the exact same
Release bytes and be made by the pinned key.

Policy
  PRESERVE the existing Release, InRelease and Release.gpg when ALL hold:
    * both signatures verify against the pinned public key, over the exact
      bytes of the existing Release (InRelease's cleartext == Release);
    * the existing Release equals the candidate Release apart from the
      freshness fields (Date, Valid-Until) -- i.e. the index hashes match;
    * its Date is not older than --max-age-days, and Valid-Until, if present,
      has not passed.
  Otherwise RENEW: install the candidate Release and sign it with --keyid.
  Renewal reasons: first publish, changed indexes, stale/expired Release,
  signature or signer mismatch (including key rotation).

A candidate that fails to verify after signing is an error, never published.

    deb-index-sign.py --repo repo --candidate-release Release.new \\
        --pubkey public.gpg --keyid KEYID [--max-age-days 7]
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

FRESHNESS_FIELDS = ("date", "valid-until")


class SignatureError(Exception):
    pass


def _gpg(home: pathlib.Path, *args: str, check: bool = True):
    return subprocess.run(
        ["gpg", "--batch", "--no-tty", "--homedir", str(home), *args],
        capture_output=True, check=check)


def trusted_fingerprints(home: pathlib.Path, pubkey: pathlib.Path) -> set[str]:
    """Import the pinned key into an isolated home; return every fingerprint
    (primary and subkeys) that may sign."""
    _gpg(home, "--import", str(pubkey))
    out = _gpg(home, "--with-colons", "--list-keys").stdout.decode()
    return {f.split(":")[9] for f in out.splitlines() if f.startswith("fpr:")}


def _verify_status(home, trusted, status_file: pathlib.Path) -> None:
    sigs = [ln.split() for ln in status_file.read_text().splitlines()
            if ln.startswith("[GNUPG:] VALIDSIG")]
    if not sigs:
        raise SignatureError("no valid signature")
    for s in sigs:
        # VALIDSIG <signing fpr> ... <primary fpr> (field 11 overall)
        if not ({s[2], s[-1]} & trusted):
            raise SignatureError(f"signed by untrusted key {s[2]}")


def verify_signatures(home, trusted, release: pathlib.Path,
                      inrelease: pathlib.Path, detached: pathlib.Path) -> None:
    """Raise SignatureError unless both forms cover exactly `release`."""
    for p in (release, inrelease, detached):
        if not p.is_file():
            raise SignatureError(f"missing {p.name}")
    with tempfile.TemporaryDirectory() as t:
        st = pathlib.Path(t) / "status"
        payload = pathlib.Path(t) / "payload"
        r = _gpg(home, "--status-file", str(st), "--output", str(payload),
                 "--decrypt", str(inrelease), check=False)
        if r.returncode != 0:
            raise SignatureError("InRelease signature does not verify")
        _verify_status(home, trusted, st)
        if payload.read_bytes() != release.read_bytes():
            raise SignatureError("InRelease payload differs from Release")
        r = _gpg(home, "--status-file", str(st), "--verify",
                 str(detached), str(release), check=False)
        if r.returncode != 0:
            raise SignatureError("Release.gpg does not verify against Release")
        _verify_status(home, trusted, st)


def parse_fields(text: str) -> dict[str, str]:
    """Top-level `Key: value` lines only (multi-line hash blocks are kept in
    order under their key)."""
    fields: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and key:
            fields[key] += "\n" + line
        elif ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            fields[key] = val.strip()
    return fields


def normalized(text: str) -> dict[str, str]:
    f = parse_fields(text)
    for k in FRESHNESS_FIELDS:
        f.pop(k, None)
    return f


def _when(value: str) -> dt.datetime:
    d = email.utils.parsedate_to_datetime(value)
    return d if d.tzinfo else d.replace(tzinfo=dt.UTC)


def stale_reason(text: str, now: dt.datetime, max_age: dt.timedelta) -> str | None:
    f = parse_fields(text)
    if "date" not in f:
        return "existing Release has no Date"
    if now - _when(f["date"]) > max_age:
        return "existing Release is older than the freshness window"
    if "valid-until" in f and _when(f["valid-until"]) <= now:
        return "existing Release has expired (Valid-Until)"
    return None


def decide(repo: pathlib.Path, candidate: pathlib.Path, home, trusted,
           now: dt.datetime, max_age: dt.timedelta) -> tuple[bool, str]:
    """(preserve, reason)."""
    release = repo / "Release"
    if not release.is_file():
        return False, "first publish: no existing Release"
    try:
        verify_signatures(home, trusted, release, repo / "InRelease",
                          repo / "Release.gpg")
    except SignatureError as e:
        return False, f"existing signatures unusable: {e}"
    old, new = release.read_text(), candidate.read_text()
    if normalized(old) != normalized(new):
        return False, "package indexes changed"
    stale = stale_reason(old, now, max_age)
    if stale:
        return False, stale
    return True, "unchanged indexes, valid signatures, within freshness window"


def renew(repo, candidate, home, trusted, keyid) -> None:
    shutil.copyfile(candidate, repo / "Release")
    for name in ("InRelease", "Release.gpg"):
        (repo / name).unlink(missing_ok=True)
    _gpg(home, "-u", keyid, "--clearsign", "-o", str(repo / "InRelease"),
         str(repo / "Release"))
    _gpg(home, "-u", keyid, "-abs", "-o", str(repo / "Release.gpg"),
         str(repo / "Release"))
    verify_signatures(home, trusted, repo / "Release", repo / "InRelease",
                      repo / "Release.gpg")


def run(repo, candidate, pubkey, keyid, max_age_days=7.0, now=None,
        signing_home=None) -> tuple[str, str]:
    """Returns ("preserved"|"renewed", reason).

    The pinned key is imported into an isolated home for verification.
    Signing uses `signing_home` (default: the caller's GNUPGHOME) because the
    private key lives there; verification never trusts that keyring."""
    now = now or dt.datetime.now(dt.UTC)
    with tempfile.TemporaryDirectory() as t:
        vhome = pathlib.Path(t)
        os.chmod(vhome, 0o700)
        trusted = trusted_fingerprints(vhome, pubkey)
        preserve, reason = decide(repo, candidate, vhome, trusted, now,
                                  dt.timedelta(days=max_age_days))
        if preserve:
            return "preserved", reason
        shome = signing_home or pathlib.Path(
            os.environ.get("GNUPGHOME", pathlib.Path.home() / ".gnupg"))
        renew(repo, candidate, shome, trusted, keyid)
        return "renewed", reason


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", type=pathlib.Path, required=True)
    ap.add_argument("--candidate-release", type=pathlib.Path, required=True)
    ap.add_argument("--pubkey", type=pathlib.Path, required=True)
    ap.add_argument("--keyid", required=True)
    ap.add_argument("--max-age-days", type=float, default=7.0)
    a = ap.parse_args(argv)
    try:
        outcome, reason = run(a.repo, a.candidate_release, a.pubkey, a.keyid,
                              a.max_age_days)
    except (SignatureError, subprocess.CalledProcessError) as e:
        print(f"::error::deb index signing failed: {e}", file=sys.stderr)
        return 1
    print(f"Release {outcome}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
