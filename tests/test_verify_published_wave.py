"""A wave is not published until the SERVED index lists it.

This is the #179 lesson: a repo that was never really published sitting
behind a green workflow. publish-tideforge-rpms.yml carries a verify job for
exactly this; publish-build-chain-rpms.yml (#463) shipped without one, so it
trusted "the workflow exited 0" for the one failure mode that phrase cannot
detect.

The check earned its place on first contact with reality. Run against the
live indexes it showed libxfce4ui-devel already served at
xfce/10-stream-x86_64 and entirely absent from repo/10/x86_64 -- which is
the el10 target's published_index, the repo tideforge buildroots actually
read. The package was never unpublished; it was published where nothing
looks. See #466.

Two behaviours have to be modelled or the check lies:

  '+' RENAMING   publish-rpm-wave.sh renames '+' to '.', so the index holds
                 the renamed name and a naive comparison reports every such
                 package missing (run 32411090239).
  SRPMS          excluded from publishing, so excluded here, or every wave
                 looks short.
"""
from __future__ import annotations

import gzip
import importlib.util
import io
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-published-wave.py"

_spec = importlib.util.spec_from_file_location("verify_wave", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)


REPOMD = b'''<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <data type="filelists"><location href="repodata/aaa-filelists.xml.gz"/></data>
  <data type="primary"><location href="repodata/bbb-primary.xml.gz"/></data>
  <data type="other"><location href="repodata/ccc-other.xml.gz"/></data>
</repomd>'''


def primary_for(*names):
    body = "".join(
        f'<package><location href="build-chain/{n}"/></package>' for n in names
    )
    return gzip.compress(f"<metadata>{body}</metadata>".encode())


def stage(tmp_path, *names):
    d = tmp_path / "staged"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_text("rpm")
    return d


def transport(primary):
    def fetch(url):
        if url.endswith("repomd.xml"):
            return REPOMD
        if url.endswith("-primary.xml.gz"):
            return primary
        raise AssertionError(f"unexpected fetch: {url}")
    return fetch


# --- what counts as published -----------------------------------------------


def test_srpms_are_not_expected_in_the_index(tmp_path) -> None:
    d = stage(tmp_path, "foo-1.0.el10.x86_64.rpm", "foo-1.0.src.rpm")
    assert verify.published_names(d) == {"foo-1.0.el10.x86_64.rpm"}


def test_plus_is_renamed_the_same_way_the_publisher_renames_it(tmp_path) -> None:
    """Otherwise every '+'-named package reports missing forever."""
    d = stage(tmp_path, "oversteer-udev-0.8.3+git74c7484.el10.noarch.rpm")
    assert verify.published_names(d) == {
        "oversteer-udev-0.8.3.git74c7484.el10.noarch.rpm"
    }


# --- reading the served index -----------------------------------------------


def test_primary_href_picks_primary_not_filelists() -> None:
    assert verify.primary_href(REPOMD) == "repodata/bbb-primary.xml.gz"


def test_primary_href_is_none_when_absent() -> None:
    assert verify.primary_href(b"<repomd></repomd>") is None


def test_indexed_names_gunzips_and_takes_basenames(monkeypatch) -> None:
    monkeypatch.setattr(verify, "fetch",
                        transport(primary_for("a-1.el10.x86_64.rpm")))
    assert verify.indexed_names("https://example.test/repo/") == {
        "a-1.el10.x86_64.rpm"
    }


# --- the verdict ------------------------------------------------------------


def test_a_fully_served_wave_passes(tmp_path, monkeypatch, capsys) -> None:
    d = stage(tmp_path, "a-1.el10.x86_64.rpm", "b-2.el10.x86_64.rpm")
    monkeypatch.setattr(verify, "fetch", transport(
        primary_for("a-1.el10.x86_64.rpm", "b-2.el10.x86_64.rpm", "old.rpm")))
    rc = verify.main(["--served-url", "https://example.test/repo/",
                      "--staged", str(d)])
    assert rc == 0
    assert "all 2 published package(s) are served" in capsys.readouterr().out


def test_a_missing_package_fails_and_is_named(tmp_path, monkeypatch, capsys) -> None:
    """The #179 shape: workflow green, package not actually reachable."""
    d = stage(tmp_path, "a-1.el10.x86_64.rpm", "ghost-9.el10.x86_64.rpm")
    monkeypatch.setattr(verify, "fetch",
                        transport(primary_for("a-1.el10.x86_64.rpm")))
    rc = verify.main(["--served-url", "https://example.test/repo/",
                      "--staged", str(d)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ghost-9.el10.x86_64.rpm" in err
    assert "did not actually receive them" in err


def test_an_unreachable_repo_fails_rather_than_passing_vacuously(
        tmp_path, monkeypatch) -> None:
    d = stage(tmp_path, "a-1.el10.x86_64.rpm")

    def boom(url):
        raise OSError("404")
    monkeypatch.setattr(verify, "fetch", boom)
    assert verify.main(["--served-url", "https://example.test/nope/",
                        "--staged", str(d)]) == 1


def test_an_empty_stage_is_an_error_not_a_pass(tmp_path) -> None:
    """Zero expected packages would otherwise satisfy any index trivially."""
    d = stage(tmp_path)
    assert verify.main(["--served-url", "https://example.test/repo/",
                        "--staged", str(d)]) == 2


# --- retry and informative error reporting -----------------------------------


def test_fetch_retries_transient_failure_and_succeeds(monkeypatch) -> None:
    calls = []
    delays = []

    def fake_urlopen(url, timeout=60):
        calls.append(url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        return io.BytesIO(b"success")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    res = verify.fetch(
        "https://example.test/repo/repodata/repomd.xml",
        attempts=4,
        sleeper=delays.append,
    )
    assert res == b"success"
    assert len(calls) == 3
    assert delays == [2, 4]


def test_fetch_exhausts_retries_and_names_exact_url(monkeypatch) -> None:
    delays = []

    def fake_urlopen(url, timeout=60):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    target_url = "https://repo.tunaos.org/gnome50/10-stream-x86_64/repodata/repomd.xml"
    with pytest.raises(RuntimeError) as exc_info:
        verify.fetch(target_url, attempts=3, sleeper=delays.append)

    assert f"could not read the served index at {target_url}: HTTP Error 403: Forbidden" in str(exc_info.value)
    assert delays == [2, 4]


def test_main_reports_failed_repomd_url_not_bare_directory(tmp_path, monkeypatch, capsys) -> None:
    d = stage(tmp_path, "a-1.el10.x86_64.rpm")
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def fake_urlopen(url, timeout=60):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rc = verify.main([
        "--served-url", "https://repo.tunaos.org/gnome50/10-stream-x86_64/",
        "--staged", str(d),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    expected_msg = (
        "ERROR: could not read the served index at "
        "https://repo.tunaos.org/gnome50/10-stream-x86_64/repodata/repomd.xml: "
        "HTTP Error 403: Forbidden\n"
    )
    assert err == expected_msg


def test_main_reports_failed_primary_url(tmp_path, monkeypatch, capsys) -> None:
    d = stage(tmp_path, "a-1.el10.x86_64.rpm")
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def fake_urlopen(url, timeout=60):
        if url.endswith("repodata/repomd.xml"):
            return io.BytesIO(REPOMD)
        if url.endswith("repodata/bbb-primary.xml.gz"):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rc = verify.main([
        "--served-url", "https://example.test/repo/",
        "--staged", str(d),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    expected_msg = (
        "ERROR: could not read the served index at "
        "https://example.test/repo/repodata/bbb-primary.xml.gz: "
        "HTTP Error 404: Not Found\n"
    )
    assert err == expected_msg


def test_main_reports_missing_primary_index_in_repomd(tmp_path, monkeypatch, capsys) -> None:
    d = stage(tmp_path, "a-1.el10.x86_64.rpm")

    def fake_urlopen(url, timeout=60):
        if url.endswith("repodata/repomd.xml"):
            return io.BytesIO(b"<repomd></repomd>")
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rc = verify.main([
        "--served-url", "https://example.test/repo/",
        "--staged", str(d),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    expected_msg = "ERROR: no primary index in https://example.test/repo/repodata/repomd.xml\n"
    assert err == expected_msg

