"""Unit tests for check_tiers.py.

Tests the build orchestration logic with mocked subprocess and file I/O.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

# Add project root to sys.path for importing check_tiers
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import check_tiers  # noqa: E402 -- needs the sys.path insert above


class TestGetPkgName:
    def test_get_pkg_name_from_spec(self, tmp_path):
        """Should return package name from rpmspec output."""
        spec_file = tmp_path / "mypkg.spec"
        spec_file.write_text("Name: mypkg\nVersion: 1.0\n")

        with patch("check_tiers.subprocess.check_output") as mock_check_output:
            mock_check_output.return_value = "mypkg\n"
            result = check_tiers.get_pkg_name(str(tmp_path))
            assert result == "mypkg"
            mock_check_output.assert_called_once()

    def test_get_pkg_name_fallback_to_dirname(self, tmp_path):
        """Should fall back to directory name when rpmspec fails."""
        spec_file = tmp_path / "mypkg.spec"
        spec_file.write_text("broken spec")

        with patch("check_tiers.subprocess.check_output") as mock_check_output:
            mock_check_output.side_effect = Exception("rpmspec failed")
            result = check_tiers.get_pkg_name(str(tmp_path))
            # Should fall back to directory basename
            assert result == tmp_path.name

    def test_get_pkg_name_skips_bootstrap_specs(self, tmp_path):
        """Should prefer non-bootstrap spec files."""
        bootstrap_spec = tmp_path / "mypkg-bootstrap.spec"
        bootstrap_spec.write_text("Name: mypkg-bootstrap\n")
        main_spec = tmp_path / "mypkg.spec"
        main_spec.write_text("Name: mypkg\n")

        with patch("check_tiers.subprocess.check_output") as mock_check_output:
            mock_check_output.return_value = "mypkg\n"
            result = check_tiers.get_pkg_name(str(tmp_path))
            # Should find the non-bootstrap spec
            assert result == "mypkg"

    def test_get_pkg_name_no_spec_files(self, tmp_path):
        """Should fall back to directory name when no .spec files exist."""
        (tmp_path / "somefile.txt").write_text("hello")

        with patch("check_tiers.subprocess.check_output") as mock_check_output:
            mock_check_output.side_effect = Exception("no spec")
            result = check_tiers.get_pkg_name(str(tmp_path))
            assert result == tmp_path.name

    def test_get_pkg_name_only_bootstrap_spec(self, tmp_path):
        """When only bootstrap spec exists, use it."""
        spec_file = tmp_path / "mypkg-bootstrap.spec"
        spec_file.write_text("Name: mypkg-bootstrap\n")

        with patch("check_tiers.subprocess.check_output") as mock_check_output:
            mock_check_output.return_value = "mypkg-bootstrap\n"
            result = check_tiers.get_pkg_name(str(tmp_path))
            assert result == "mypkg-bootstrap"


class TestGetStatus:
    def test_get_status_returns_map(self):
        """Should return a dict keyed by (pkg, chroot) with state values."""
        mock_json = json.dumps([
            {"name": "pkg1", "chroot": "epel-10-aarch64", "state": "succeeded"},
            {"name": "pkg1", "chroot": "alma-kitten+epel-10-x86_64_v2", "state": "succeeded"},
            {"name": "pkg2", "chroot": "epel-10-aarch64", "state": "failed"},
        ])

        with patch("check_tiers.subprocess.check_output", return_value=mock_json):
            result = check_tiers.get_status()

        assert ("pkg1", "epel-10-aarch64") in result
        assert ("pkg1", "alma-kitten+epel-10-x86_64_v2") in result
        assert ("pkg2", "epel-10-aarch64") in result
        assert result[("pkg1", "epel-10-aarch64")] == "succeeded"
        assert result[("pkg2", "epel-10-aarch64")] == "failed"

    def test_get_status_takes_first_entry(self):
        """When duplicate entries exist, should take the first one."""
        mock_json = json.dumps([
            {"name": "pkg1", "chroot": "epel-10-aarch64", "state": "succeeded"},
            {"name": "pkg1", "chroot": "epel-10-aarch64", "state": "failed"},
        ])

        with patch("check_tiers.subprocess.check_output", return_value=mock_json):
            result = check_tiers.get_status()

        assert result[("pkg1", "epel-10-aarch64")] == "succeeded"
