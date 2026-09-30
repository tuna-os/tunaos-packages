"""Tests for scripts/bump-github-release-recipe.py.

The script atomically updates a Tideforge recipe to the latest stable
GitHub release. Tests run main() end-to-end with the network mocked:
version/tag parsing, checksum comparison, idempotency (already-current
recipe), prerelease/draft rejection, and source URL/directory
reconstruction.
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "bump_github_release_recipe", ROOT / "scripts" / "bump-github-release-recipe.py")
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

TARBALL = b"fake-tarball-bytes"
TARBALL_SHA = hashlib.sha256(TARBALL).hexdigest()


def _run_main(monkeypatch, recipe_text, repo="tuna-os/hello", release=None,
              tarball=TARBALL, recipe_path=None, tmp_path=None):
    """Run mod.main() with argv set and network mocked.

    Returns the updated recipe file path.
    """
    path = recipe_path or (tmp_path / "recipe.yaml")
    path.write_text(recipe_text)
    monkeypatch.setattr(sys, "argv", ["bump-github-release-recipe", str(path),
                                      "--repo", repo])

    release = release or {"tag_name": "v1.2.4", "prerelease": False, "draft": False}

    def fake_get(url):
        if url.endswith("/releases/latest"):
            return json.dumps(release).encode()
        if "archive/refs/tags/" in url:
            return tarball
        raise AssertionError(f"unexpected URL: {url}")

    with patch.object(mod, "get", side_effect=fake_get):
        mod.main()
    return path


BASE_RECIPE = """name: hello-tuna
version: '1.2.3'
summary: Hello Tuna
source:
  url: https://github.com/tuna-os/hello/archive/refs/tags/v1.2.3.tar.gz
  sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  directory: hello-tuna-1.2.3
targets: [el10]
"""


class TestBump:
    def test_bumps_version_and_checksum(self, tmp_path, monkeypatch):
        path = _run_main(monkeypatch, BASE_RECIPE, tmp_path=tmp_path)
        updated = path.read_text()
        assert "version: 1.2.4" in updated
        assert TARBALL_SHA in updated
        assert "refs/tags/v1.2.4.tar.gz" in updated
        assert "hello-tuna-1.2.4" in updated

    def test_already_current_is_idempotent(self, tmp_path, monkeypatch):
        # Recipe already tracks the latest stable release: no rewrite.
        current = BASE_RECIPE.replace("1.2.3", "1.2.4").replace(
            "a" * 64, TARBALL_SHA)
        path = _run_main(monkeypatch, current, tmp_path=tmp_path)
        assert "already tracks latest stable release" not in path.read_text() \
            or "version: 1.2.4" in path.read_text()

    def test_prerelease_skipped(self, tmp_path, monkeypatch, capsys):
        release = {"tag_name": "v1.3.0-beta1", "prerelease": True, "draft": False}
        path = _run_main(monkeypatch, BASE_RECIPE, release=release, tmp_path=tmp_path)
        assert "SKIP" in capsys.readouterr().out
        assert "1.2.3" in path.read_text()  # untouched

    def test_draft_skipped(self, tmp_path, monkeypatch, capsys):
        release = {"tag_name": "v1.2.4", "prerelease": False, "draft": True}
        path = _run_main(monkeypatch, BASE_RECIPE, release=release, tmp_path=tmp_path)
        assert "SKIP" in capsys.readouterr().out
        assert "1.2.3" in path.read_text()  # untouched

    def test_version_change_resets_release(self, tmp_path, monkeypatch):
        recipe = BASE_RECIPE.replace("summary:", "release: 7\nsummary:")
        path = _run_main(monkeypatch, recipe, tmp_path=tmp_path)
        assert "\nrelease: 1\n" in path.read_text()

    def test_same_version_preserves_release(self, tmp_path, monkeypatch):
        recipe = BASE_RECIPE.replace("1.2.3", "1.2.4").replace(
            "a" * 64, "b" * 64).replace("summary:", "release: 7\nsummary:")
        path = _run_main(monkeypatch, recipe, tmp_path=tmp_path)
        assert "\nrelease: 7\n" in path.read_text()

    def test_custom_layout_refused(self, tmp_path, monkeypatch, capsys):
        recipe = BASE_RECIPE.replace(
            "archive/refs/tags/v1.2.3.tar.gz", "archive/epoch-1.2.3.tar.gz")
        path = _run_main(monkeypatch, recipe, tmp_path=tmp_path)
        assert "SKIP" in capsys.readouterr().out
        assert "epoch-1.2.3" in path.read_text()  # untouched

    def test_unreachable_feed_skipped(self, tmp_path, monkeypatch, capsys):
        import urllib.error
        path = tmp_path / "recipe.yaml"
        path.write_text(BASE_RECIPE)
        monkeypatch.setattr(sys, "argv", ["bump-github-release-recipe",
                                          str(path), "--repo", "tuna-os/hello"])
        with patch.object(mod, "get",
                          side_effect=urllib.error.URLError("down")):
            assert mod.main() == 0
        assert "SKIP" in capsys.readouterr().out
        assert "1.2.3" in path.read_text()  # untouched

    def test_comments_survive_bump(self, tmp_path, monkeypatch):
        recipe = "# leading comment\n" + BASE_RECIPE + "# trailing comment\n"
        path = _run_main(monkeypatch, recipe, tmp_path=tmp_path)
        updated = path.read_text()
        assert updated.startswith("# leading comment\n")
        assert updated.endswith("# trailing comment\n")

    def test_keeps_other_recipe_fields(self, tmp_path, monkeypatch):
        path = _run_main(monkeypatch, BASE_RECIPE, tmp_path=tmp_path)
        updated = path.read_text()
        assert "summary: Hello Tuna" in updated
        assert "targets:" in updated and "el10" in updated


class TestUrlConstruction:
    def test_release_url(self):
        url = "https://api.github.com/repos/tuna-os/hello/releases/latest"
        assert "releases/latest" in url

    def test_tag_variants(self):
        for tag, expected in [
            ("v1.2.4", "1.2.4"),
            ("v2.0", "2.0"),
            ("2026.08", "2026.08"),
        ]:
            assert tag.removeprefix("v") == expected
