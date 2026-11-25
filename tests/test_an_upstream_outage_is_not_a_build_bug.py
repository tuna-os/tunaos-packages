"""An unreadable upstream is its own class, never a build bug (#489).

Signatures come from real incidents: openSUSE mirror skew (#489), Fedora
`Curl error (23)` (#526), hummingbird `All mirrors were tried` (#529) and
Docker Hub pull exhaustion (#284). Each must classify as `upstream-fetch`
and name the host, so the gate summary can say "upstream unavailable
(infra)" and keep the cell red.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "classify-chain-failures.py"
_spec = importlib.util.spec_from_file_location("classify", SCRIPT)
classify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(classify)

CASES = {
    "zypper-skew": (
        "Download (curl) error for 'https://download.opensuse.org/tumbleweed/repo/oss/x86_64/foo.rpm':\n"
        "File 'x86_64/foo.rpm' not found on medium 'https://download.opensuse.org/tumbleweed/repo/oss'\n",
        "download.opensuse.org",
    ),
    "zypper-metadata": (
        "Failed to retrieve new repository metadata from https://download.opensuse.org/x\n",
        "download.opensuse.org",
    ),
    "curl-23": (
        "Curl error (23): Failed writing received data to disk/application for "
        "https://mirror.fcix.net/fedora/repodata/abc-primary.xml.zck\n",
        "mirror.fcix.net",
    ),
    "librepo": (
        "Librepo error: Cannot download Packages/c/cpio.rpm: All mirrors were tried\n"
        "https://public-hummingbird.example/repo\n",
        "public-hummingbird.example",
    ),
    "docker-hub": (
        "Error: toomanyrequests: You have reached your pull rate limit "
        "https://registry-1.docker.io/v2/\n",
        "registry-1.docker.io",
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_signature_is_upstream_fetch_and_names_the_host(tmp_path, name):
    text, host = CASES[name]
    (tmp_path / "pkg.root.log").write_text(text)
    report = classify.analyse_names(["pkg"], tmp_path)
    record = report["records"][0]
    assert record["class"] == "upstream-fetch"
    assert "upstream unavailable (infra)" in record["why"]
    assert host in record["why"]


def test_a_real_compile_error_is_not_upstream(tmp_path):
    (tmp_path / "pkg.build.log").write_text("foo.c:3: error: expected ';'\n")
    assert classify.analyse_names(["pkg"], tmp_path)["records"][0]["class"] == "compile-error"


def test_a_retried_curl_warning_does_not_mask_a_real_failure(tmp_path):
    (tmp_path / "pkg.root.log").write_text(
        "Curl error (28): Timeout for https://mirror.example/repodata/x.xml\n"
        "(retrying on next mirror, ok)\n"
    )
    (tmp_path / "pkg.build.log").write_text("foo.c:3: error: expected ';'\n")
    assert classify.analyse_names(["pkg"], tmp_path)["records"][0]["class"] == "compile-error"
