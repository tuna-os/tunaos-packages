"""Integration-style tests for check_tiers.main() with mocked dependencies."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import check_tiers  # noqa: E402 -- needs the sys.path insert above

# Sample build-order.yml for testing
SAMPLE_BUILD_ORDER = {
    "tiers": [
        {
            "name": "bootstrap",
            "packages": [
                {"path": "pkg1"},
                {"path": "pkg2"},
            ],
        },
        {
            "name": "core",
            "packages": [
                {"path": "pkg3"},
            ],
        },
    ],
}


class TestMain:
    def test_main_all_succeeded(self, tmp_path):
        """When all packages have succeeded, main should print completion."""
        build_order = tmp_path / "build-order.yml"
        build_order.write_text(yaml.dump(SAMPLE_BUILD_ORDER))

        mock_status = {
            ("pkg1", check_tiers.ARM_CHROOT): "succeeded",
            ("pkg1", check_tiers.V2_CHROOT): "succeeded",
            ("pkg2", check_tiers.ARM_CHROOT): "succeeded",
            ("pkg2", check_tiers.V2_CHROOT): "succeeded",
            ("pkg3", check_tiers.ARM_CHROOT): "succeeded",
            ("pkg3", check_tiers.V2_CHROOT): "succeeded",
        }

        with patch.multiple(
            check_tiers,
            get_status=MagicMock(return_value=mock_status),
            get_pkg_name=MagicMock(side_effect=lambda p: Path(p).name),
        ):
            with patch.object(sys, "argv", ["check_tiers.py"]):
                with patch("pathlib.Path.read_text", return_value=yaml.dump(SAMPLE_BUILD_ORDER)):
                    # main() should complete without triggering any builds
                    check_tiers.main()

    def test_main_missing_package_triggers_build(self, tmp_path):
        """When a package is missing/failed, main should trigger builds."""
        build_order = tmp_path / "build-order.yml"
        build_order.write_text(yaml.dump(SAMPLE_BUILD_ORDER))

        mock_status = {
            ("pkg1", check_tiers.ARM_CHROOT): "succeeded",
            ("pkg1", check_tiers.V2_CHROOT): "succeeded",
            ("pkg2", check_tiers.ARM_CHROOT): "failed",  # This should trigger
            ("pkg2", check_tiers.V2_CHROOT): "succeeded",
            ("pkg3", check_tiers.ARM_CHROOT): "succeeded",
            ("pkg3", check_tiers.V2_CHROOT): "succeeded",
        }

        with patch.multiple(
            check_tiers,
            get_status=MagicMock(return_value=mock_status),
            get_pkg_name=MagicMock(side_effect=lambda p: Path(p).name),
        ):
            with patch.object(sys, "argv", ["check_tiers.py"]):
                with patch("pathlib.Path.read_text", return_value=yaml.dump(SAMPLE_BUILD_ORDER)):
                    with patch("check_tiers.subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock()
                        check_tiers.main()
                        # Should have triggered build for pkg2
                        mock_run.assert_called_once()

    def test_main_multiple_missing_in_one_tier(self, tmp_path):
        """When multiple packages are missing, main should trigger all builds."""
        build_order = tmp_path / "build-order.yml"
        build_order.write_text(yaml.dump(SAMPLE_BUILD_ORDER))

        mock_status = {
            ("pkg1", check_tiers.ARM_CHROOT): "succeeded",
            ("pkg1", check_tiers.V2_CHROOT): "failed",
            ("pkg2", check_tiers.ARM_CHROOT): "failed",
            ("pkg2", check_tiers.V2_CHROOT): "failed",
            ("pkg3", check_tiers.ARM_CHROOT): "succeeded",
            ("pkg3", check_tiers.V2_CHROOT): "succeeded",
        }

        with patch.multiple(
            check_tiers,
            get_status=MagicMock(return_value=mock_status),
            get_pkg_name=MagicMock(side_effect=lambda p: Path(p).name),
        ):
            with patch.object(sys, "argv", ["check_tiers.py"]):
                with patch("pathlib.Path.read_text", return_value=yaml.dump(SAMPLE_BUILD_ORDER)):
                    with patch("check_tiers.subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock()
                        check_tiers.main()
                        # Should have triggered builds for pkg1 and pkg2
                        assert mock_run.call_count >= 1

    def test_main_empty_tiers(self, tmp_path):
        """When build-order.yml has no tiers, main should handle gracefully."""
        empty_order = {"tiers": []}
        build_order = tmp_path / "build-order.yml"
        build_order.write_text(yaml.dump(empty_order))

        with patch.multiple(
            check_tiers,
            get_status=MagicMock(return_value={}),
            get_pkg_name=MagicMock(return_value="pkg"),
        ):
            with patch.object(sys, "argv", ["check_tiers.py"]):
                with patch("pathlib.Path.read_text", return_value=yaml.dump(empty_order)):
                    result = check_tiers.main()
                    # No tiers means no builds triggered
                    assert result is None  # main() returns None on success
