import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_gnome_desktop_experience",
    ROOT / "scripts" / "verify-gnome-desktop-experience.py",
)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def test_accepts_a_complete_gnome_desktop_package_set() -> None:
    installed = [f"{package} 1" for package in checker.REQUIRED_GNOME_PACKAGES]
    assert checker.missing_packages(installed) == []


def test_rejects_a_session_only_gnome_install() -> None:
    installed = ["gdm", "gnome-session", "gnome-shell", "mutter"]
    assert checker.missing_packages(installed) == [
        "gnome-keyring",
        "gvfs",
        "nautilus",
        "xdg-desktop-portal-gnome",
    ]


def test_ignores_empty_lines_and_metadata_columns() -> None:
    installed = ["", "gnome-shell 50.1", "gdm 49.0", "mutter"]
    assert checker.missing_packages(installed, ["gnome-shell", "gdm", "mutter"]) == []
