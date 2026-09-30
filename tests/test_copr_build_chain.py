"""Tests for scripts/copr-build-chain.py

Tests the core pure-logic functions (resolve_pkg, get_rpm_name) using
temporary spec files and directories. Avoids testing functions that
call COPR CLI or external build infrastructure.
"""

import os
import tempfile
from pathlib import Path


def _import_module():
    """Import copr-build-chain module from scripts directory."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "copr_build_chain",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "copr-build-chain.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _create_spec(tmpdir: Path, filename: str, name: str = None) -> Path:
    """Create a minimal .spec file returning the path."""
    if name is None:
        name = Path(filename).stem
    spec_path = tmpdir / filename
    spec_path.write_text(
        f"Name:       {name}\n"
        f"Version:    1.0\n"
        f"Release:    1%{{?dist}}\n"
        f"License:    MIT\n"
        f"Summary:    Test package\n"
        f"%description\nTest description.\n"
    )
    return spec_path


class TestGetRpmName:
    """Test extraction of RPM Name from spec files."""

    def test_basic_spec(self):
        """Standard spec file with Name field."""
        mod = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = _create_spec(Path(tmpdir), "glib2.spec", name="glib2")
            result = mod.get_rpm_name(str(spec))
            assert result == "glib2", f"Expected 'glib2', got {result!r}"

    def test_spec_with_dashes(self):
        """Spec with name containing dashes."""
        mod = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = _create_spec(Path(tmpdir), "gnome-shell.spec", name="gnome-shell")
            result = mod.get_rpm_name(str(spec))
            assert result == "gnome-shell", f"Expected 'gnome-shell', got {result!r}"

    def test_spec_with_plus_in_name(self):
        """Spec with name containing plus sign."""
        mod = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = _create_spec(Path(tmpdir), "gtk4.spec", name="gtk4+extra")
            result = mod.get_rpm_name(str(spec))
            assert result == "gtk4+extra", f"Expected 'gtk4+extra', got {result!r}"

    def test_missing_spec_file(self):
        """Missing spec file raises FileNotFoundError (no graceful handling yet)."""
        mod = _import_module()
        import pytest
        with pytest.raises(FileNotFoundError):
            mod.get_rpm_name("/nonexistent/path/pkg.spec")

    def test_spec_with_extra_whitespace(self):
        """Spec Name field with extra whitespace is stripped."""
        mod = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = Path(tmpdir) / "mutter.spec"
            spec.write_text("Name:\tmutter\nVersion: 42.0\n")
            result = mod.get_rpm_name(str(spec))
            assert result == "mutter", f"Expected 'mutter', got {result!r}"


class TestResolvePkg:
    """Test package resolution from path/spec_override/copr_name."""

    def test_copr_name_override(self):
        """copr_name_override bypasses spec lookup entirely."""
        mod = _import_module()
        result = mod.resolve_pkg(
            pkg_path="/some/path",
            spec_override=None,
            copr_name_override="gnome-shell",
        )
        assert result == ("gnome-shell", None, "gnome-shell"), (
            f"Unexpected result: {result}"
        )

    def test_spec_override_bootstrap(self):
        """spec_override derives COPR name from spec filename stem."""
        mod = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            _create_spec(d, "glib2-bootstrap.spec", name="glib2")
            result = mod.resolve_pkg(str(d), spec_override="glib2-bootstrap.spec")
            # copr_pkg_name = stem of spec_override = "glib2-bootstrap"
            assert result is not None, "Expected a result, got None"
            copr_name, spec_file, rpm_name = result
            assert copr_name == "glib2-bootstrap", (
                f"Expected COPR name 'glib2-bootstrap', got {copr_name!r}"
            )
            assert spec_file is not None and spec_file.endswith("glib2-bootstrap.spec")
            assert rpm_name == "glib2", (
                f"Expected RPM name 'glib2', got {rpm_name!r}"
            )

    def test_no_override_picks_first_spec(self):
        """No spec_override picks the first non-bootstrap, non-rawhide spec."""
        mod = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            # Create multiple specs — should pick the non-bootstrap one
            _create_spec(d, "glib2-bootstrap.spec", name="glib2")
            _create_spec(d, "glib2.spec", name="glib2")
            result = mod.resolve_pkg(str(d), spec_override=None)
            assert result is not None, "Expected a result, got None"
            copr_name, spec_file, rpm_name = result
            assert copr_name == "glib2", (
                f"Expected COPR name 'glib2', got {copr_name!r}"
            )
            assert "bootstrap" not in spec_file, (
                f"Should not pick bootstrap spec, got {spec_file}"
            )

    def test_only_bootstrap_spec_available(self):
        """When only bootstrap specs exist, picks the first one."""
        mod = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            _create_spec(d, "glib2-bootstrap.spec", name="glib2")
            result = mod.resolve_pkg(str(d), spec_override=None)
            assert result is not None, "Expected a result, got None"
            copr_name, _, rpm_name = result
            assert copr_name == "glib2", f"Expected 'glib2', got {copr_name!r}"
            assert rpm_name == "glib2", f"Expected 'glib2', got {rpm_name!r}"

    def test_no_specs_at_all(self):
        """Directory with no .spec files returns (None, None, None)."""
        mod = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            result = mod.resolve_pkg(str(d), spec_override=None)
            assert result == (None, None, None), (
                f"Expected (None, None, None), got {result}"
            )

    def test_spec_override_file_not_found(self):
        """Spec override referencing a missing file returns (None, None, None)."""
        mod = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            result = mod.resolve_pkg(str(d), spec_override="missing.spec")
            assert result == (None, None, None), (
                f"Expected (None, None, None) for missing spec, got {result}"
            )

    def test_different_rpm_name_from_spec(self):
        """When spec filename and RPM Name differ, COPR name follows the rule."""
        mod = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            _create_spec(d, "weird-filename.spec", name="actual-rpm-name")
            result = mod.resolve_pkg(str(d), spec_override=None)
            assert result is not None, "Expected a result, got None"
            copr_name, _, rpm_name = result
            assert rpm_name == "actual-rpm-name", (
                f"Expected rpm_name 'actual-rpm-name', got {rpm_name!r}"
            )
            # Without spec_override, copr_name = rpm_name
            assert copr_name == "actual-rpm-name", (
                f"Expected copr_name 'actual-rpm-name', got {copr_name!r}"
            )
