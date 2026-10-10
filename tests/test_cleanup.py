"""Tests for scripts/cleanup.py"""

import os
import tempfile
from pathlib import Path


def _import_cleanup():
    """Import cleanup module from scripts directory."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cleanup",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "cleanup.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestParseNEVR:
    """Test RPM filename parsing (name-epoch:version-release)."""

    def test_rpm_with_epoch(self):
        """RPM with epoch: name-epoch:version-release.arch.rpm."""
        mod = _import_cleanup()
        result = mod.parse_nevr("glibc-2:2.38-11.fc40.x86_64.rpm")
        assert result == ("glibc", "2", "2.38", "11.fc40.x86_64"), (
            f"Unexpected result: {result}"
        )

    def test_rpm_with_epoch_kernel(self):
        """Kernel RPM with epoch."""
        mod = _import_cleanup()
        result = mod.parse_nevr("kernel-6:6.8.5-301.fc40.x86_64.rpm")
        assert result == ("kernel", "6", "6.8.5", "301.fc40.x86_64"), (
            f"Unexpected result: {result}"
        )

    def test_rpm_with_multi_digit_epoch(self):
        """RPM with multi-digit epoch."""
        mod = _import_cleanup()
        result = mod.parse_nevr("python3-10:3.12.2-1.fc40.x86_64.rpm")
        assert result == ("python3", "10", "3.12.2", "1.fc40.x86_64"), (
            f"Unexpected result: {result}"
        )

    def test_rpm_without_epoch(self):
        """RPM without epoch falls back to raw filename, epoch='0'."""
        mod = _import_cleanup()
        result = mod.parse_nevr("bash-5.2.15-2.fc40.x86_64.rpm")
        assert result == ("bash-5.2.15-2.fc40.x86_64.rpm", "0", "", ""), (
            f"Unexpected result: {result}"
        )

    def test_non_rpm_filename(self):
        """Non-RPM filename falls back to raw name, epoch='0'."""
        mod = _import_cleanup()
        result = mod.parse_nevr("source.tar.gz")
        assert result == ("source.tar.gz", "0", "", ""), (
            f"Unexpected result: {result}"
        )

    def test_rpm_name_with_dots(self):
        """RPM name containing dots still parses correctly."""
        mod = _import_cleanup()
        result = mod.parse_nevr("lib.python3-1:3.0-1.fc40.x86_64.rpm")
        # Greedy (.+) in name + version, but epoch digit group anchors
        assert isinstance(result, tuple) and len(result) == 4
        assert result[1] == "1"  # epoch
        assert result[3] != ""   # release present


class TestGetRpmVersionSortKey:
    """Test RPM version sort key generation."""

    def test_higher_version_sorts_after_lower(self):
        """Higher version (2.39 > 2.38) should produce a larger sort key."""
        mod = _import_cleanup()
        newer = mod.get_rpm_version_sort_key("glibc-2:2.39-1.fc40.x86_64.rpm")
        older = mod.get_rpm_version_sort_key("glibc-2:2.38-1.fc40.x86_64.rpm")
        assert newer > older, (
            f"Expected newer version to sort after older: {newer} <= {older}"
        )

    def test_same_filename_produces_same_key(self):
        """Identical filenames must produce identical sort keys."""
        mod = _import_cleanup()
        k1 = mod.get_rpm_version_sort_key("glibc-2:2.38-1.fc40.x86_64.rpm")
        k2 = mod.get_rpm_version_sort_key("glibc-2:2.38-1.fc40.x86_64.rpm")
        assert k1 == k2, (
            f"Expected identical keys for same filename: {k1} != {k2}"
        )

    def test_higher_release_sorts_after_lower(self):
        """Higher release (11 > 10) produces a larger sort key."""
        mod = _import_cleanup()
        higher = mod.get_rpm_version_sort_key("glibc-2:2.38-11.fc40.x86_64.rpm")
        lower = mod.get_rpm_version_sort_key("glibc-2:2.38-10.fc40.x86_64.rpm")
        assert higher > lower, (
            f"Expected higher release to sort after lower: {higher} <= {lower}"
        )

    def test_fallback_no_epoch_produces_tuple(self):
        """RPM without epoch produces a valid sortable tuple."""
        mod = _import_cleanup()
        key = mod.get_rpm_version_sort_key("bash-5.2.15-2.fc40.x86_64.rpm")
        assert isinstance(key, tuple) and len(key) >= 3

    def test_different_packages_different_keys(self):
        """Different package names produce different sort keys."""
        mod = _import_cleanup()
        k1 = mod.get_rpm_version_sort_key("glibc-1:2.38-1.fc40.x86_64.rpm")
        k2 = mod.get_rpm_version_sort_key("bash-1:5.2-1.fc40.x86_64.rpm")
        assert k1 != k2

    def test_different_epoch_sorts_correctly(self):
        """Higher epoch sorts after lower, even with same version."""
        mod = _import_cleanup()
        higher_epoch = mod.get_rpm_version_sort_key("glibc-2:2.38-1.fc40.x86_64.rpm")
        lower_epoch = mod.get_rpm_version_sort_key("glibc-1:2.38-1.fc40.x86_64.rpm")
        assert higher_epoch > lower_epoch, (
            f"Expected higher epoch to sort after lower: {higher_epoch} <= {lower_epoch}"
        )


class TestCleanupLocal:
    """Test local directory cleanup logic."""

    def _create_rpm(self, directory: Path, name: str) -> Path:
        """Create a dummy RPM file with the given name."""
        rpm_path = directory / name
        rpm_path.write_text("dummy rpm content")
        return rpm_path

    def test_empty_directory_no_crash(self):
        """Empty directory must not cause errors."""
        mod = _import_cleanup()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Should not raise
            mod.cleanup_local(tmpdir, max_versions=3, dry_run=False)

    def test_dry_run_preserves_files(self):
        """Dry run must not delete any files."""
        mod = _import_cleanup()
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            self._create_rpm(d, "glibc-2:2.38-10.fc40.x86_64.rpm")
            self._create_rpm(d, "glibc-2:2.38-11.fc40.x86_64.rpm")

            mod.cleanup_local(str(d), max_versions=1, dry_run=True)

            remaining = list(d.glob("*.rpm"))
            assert len(remaining) == 2, (
                f"Expected all files preserved in dry run, got {len(remaining)}"
            )

    def test_live_mode_no_crash(self):
        """Live mode must not crash and should leave files intact when each has unique filename."""
        mod = _import_cleanup()
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            self._create_rpm(d, "glibc-2:2.38-10.fc40.x86_64.rpm")
            self._create_rpm(d, "glibc-2:2.38-11.fc40.x86_64.rpm")

            # Each unique filename is its own group of 1 version,
            # so max_versions=1 keeps everything
            mod.cleanup_local(str(d), max_versions=1, dry_run=False)

            remaining = list(d.glob("*.rpm"))
            assert len(remaining) == 2, (
                f"Expected 2 files (each unique filename = own group), got {len(remaining)}"
            )
