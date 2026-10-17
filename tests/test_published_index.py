"""The published_index contract: one arch, possibly several served indexes.

#467: el10 has two publishers writing to two disjoint prefixes —
publish-tideforge-rpms.yml into repo/10-x86_64 (served /repo/10/x86_64/) and
publish-build-chain-rpms.yml into each family's own prefix. Only the first
was ever declared, so every build-chain product was invisible to every
buildroot: libxfce4ui-devel had been built and SERVED the whole time, at
xfce/10-stream-x86_64, where nothing looked. tideforge-xfwl4-el10 was
blocked on the READER, not on the package.

What these tests pin:

  SHAPE       a bare string still means one index (most targets have one,
              and rewriting them into single-item lists would be noise);
  UNION       BUILT is the union across a target's indexes, because a
              buildroot pointed at that target is pointed at all of them;
  PLUMBING    both cell scripts resolve through this one module — the third
              and fourth hand-copied YAML snippet is how the single-URL
              assumption survived being wrong;
  RESTRAINT   the gnome49/gnome51 prefixes stay undeclared. An index is
              ADDITIVE to a buildroot's package universe and those carry
              GNOME 50/51 bootstrap glib2 builds whose
              `Obsoletes: glib2 < 2.87.3` hijacks the AppStream package at
              any repo priority (publish run 32405815822).
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import published_index as pubidx  # noqa: E402

MANIFEST = ROOT / "manifests" / "package-factory.yaml"
RUNNER = ROOT / "scripts" / "run-package-factory-cell.sh"
VERIFIER = ROOT / "scripts" / "verify-package-factory-cell.sh"


def _factory():
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- shape


def test_a_bare_string_is_one_index():
    assert pubidx.normalise("https://example.test/a/") == ["https://example.test/a/"]


def test_a_list_keeps_its_order():
    urls = ["https://example.test/a/", "https://example.test/b/"]
    assert pubidx.normalise(urls) == urls


def test_absent_and_blank_resolve_to_nothing():
    assert pubidx.normalise(None) == []
    assert pubidx.normalise([]) == []
    # A commented-out URL must not become an empty baseurl, which dnf reads
    # as the current directory.
    assert pubidx.normalise(["", "  "]) == []


def test_a_wrong_type_is_an_error_not_a_silent_empty():
    with pytest.raises(TypeError):
        pubidx.normalise({"x86_64": "https://example.test/"})


def test_urls_for_reads_the_target_contract():
    target = {"published_index": {"x86_64": ["https://a.test/", "https://b.test/"]}}
    assert pubidx.urls_for(target, "x86_64") == ["https://a.test/", "https://b.test/"]
    assert pubidx.urls_for(target, "aarch64") == []
    assert pubidx.urls_for({}, "x86_64") == []


# ------------------------------------------------------------------ manifest


def test_el10_x86_64_reads_both_of_its_publishers():
    """The fix itself: the build-chain xfce prefix is declared alongside the
    tideforge mirror. Without the second, xfwl4's BuildRequires
    (libxfce4ui-devel, xfconf-devel) resolve from nowhere."""
    el10 = _factory()["targets"]["el10"]
    urls = pubidx.urls_for(el10, "x86_64")
    assert "https://repo.tunaos.org/repo/10/x86_64/" in urls
    assert "https://repo.tunaos.org/xfce/10-stream-x86_64/" in urls


def test_the_glib2_poisoning_prefixes_stay_undeclared():
    """Restraint is part of the contract, so it gets a test.

    gnome49/gnome51/10-stream-x86_64 hold bootstrap glib2 builds that
    obsolete the AppStream package regardless of repo priority. Declaring
    an index makes a buildroot SEE it; these must not be added without the
    stale packages being cleaned out first."""
    declared = " ".join(
        url
        for target in _factory()["targets"].values()
        for value in (target.get("published_index") or {}).values()
        for url in pubidx.normalise(value)
    )
    assert "gnome49" not in declared
    assert "gnome50" not in declared
    assert "gnome51" not in declared


def test_every_declared_index_is_absolute_and_directory_shaped():
    for target_id, spec in _factory()["targets"].items():
        for arch, value in (spec.get("published_index") or {}).items():
            for url in pubidx.normalise(value):
                assert url.startswith("https://"), (target_id, arch, url)
                # rpm-md and flat-apt readers both append repodata/ or
                # Packages.gz to this, so it must name a directory.
                assert url.endswith("/"), (target_id, arch, url)


# ------------------------------------------------------------------ plumbing


@pytest.mark.parametrize("script", [RUNNER, VERIFIER], ids=["runner", "verifier"])
def test_cell_scripts_resolve_through_the_shared_module(script):
    """No hand-copied YAML snippet may come back.

    Both scripts used to carry their own four-line `python3 - <<PY` reader,
    and both hard-coded `.get(arch, "")` — a string. That duplication is
    exactly why the single-URL assumption survived: fixing one would have
    left the other wrong."""
    text = script.read_text()
    assert "scripts/published_index.py" in text
    assert 'get("published_index")' not in text


@pytest.mark.parametrize("script", [RUNNER, VERIFIER], ids=["runner", "verifier"])
def test_cell_scripts_loop_over_every_url(script):
    """A second index must produce a second repo, not a repo whose baseurl
    is two URLs joined by a space."""
    text = script.read_text()
    assert "for published_url in ${PUBLISHED_INDEX:-}" in text


def test_the_resolver_cli_prints_one_url_per_line(tmp_path):
    manifest = tmp_path / "package-factory.yaml"
    manifest.write_text(
        "targets:\n"
        "  el10:\n"
        "    published_index:\n"
        "      x86_64:\n"
        "        - https://a.test/\n"
        "        - https://b.test/\n"
        "      aarch64: https://c.test/\n",
        encoding="utf-8",
    )
    def run(*args):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "published_index.py"),
             *args, "--manifest", str(manifest)],
            check=True, stdout=subprocess.PIPE, text=True,
        ).stdout
    assert run("el10", "x86_64") == "https://a.test/\nhttps://b.test/\n"
    assert run("el10", "x86_64", "--join") == "https://a.test/ https://b.test/\n"
    assert run("el10", "aarch64", "--join") == "https://c.test/\n"
    # An undeclared arch prints an empty line, which the shell reads as the
    # empty string — the same "adds nothing" the single-URL reader gave.
    assert run("el10", "ppc64le", "--join") == "\n"
    assert run("nosuchtarget", "x86_64", "--join") == "\n"
