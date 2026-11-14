"""The deb verify job's evidence must be what the receipt consumes.

ubuntu:26.04 ships /etc/dpkg/dpkg.cfg.d/excludes, which path-excludes
/usr/share/doc, man pages and .mo catalogues. deb-installed-payload.sh compares
every archive member with the installed root, so the ubuntu verify job failed
with "missing installed file: usr/share/doc/tuna-desktop/nested-session.md.gz"
on a correctly published package. This runs the script's lift block against a
copy of that dpkg configuration and checks it runs before any package install.

installed.tsv carries the archive basename: deb-verification-receipt.py
compares it with the ActionResult artifact name and with the basename of the
served Filename, and refused the full "pool/..." path the script used to write.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "deb-verify-published.sh"

UBUNTU_EXCLUDES = """\
# Drop all man pages
path-exclude=/usr/share/man/*

# Drop all translations
path-exclude=/usr/share/locale/*/LC_MESSAGES/*.mo

# Drop all documentation ...
path-exclude=/usr/share/doc/*

# ... except copyright files ...
path-include=/usr/share/doc/*/copyright

# ... and Debian changelogs for native & non-native packages
path-include=/usr/share/doc/*/changelog.*
"""


def lift_block() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("for dpkg_cfg in /etc/dpkg/dpkg.cfg.d/*; do")
    end = text.index("\nfi\n", text.index("dpkg path-exclude still active")) + len("\nfi\n")
    return text[start:end]


def run_lift(dpkg_root: Path) -> subprocess.CompletedProcess[str]:
    block = lift_block().replace("/etc/dpkg", str(dpkg_root))
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + block],
        capture_output=True,
        text=True,
    )


def test_the_lift_runs_before_any_package_is_unpacked():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.index("for dpkg_cfg in /etc/dpkg/dpkg.cfg.d/*") < text.index("apt-get install")


def test_installed_tsv_names_the_archive_not_the_pool_path():
    text = SCRIPT.read_text(encoding="utf-8")
    record = text[text.index("printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n'"):text.index('> "$observed/installed.tsv"')]
    assert '"$archive_name"' in record
    assert '"$filename"' not in record



def test_the_ubuntu_image_exclusions_are_lifted(tmp_path):
    conf = tmp_path / "dpkg.cfg.d"
    conf.mkdir()
    (tmp_path / "dpkg.cfg").write_text("# dpkg defaults\n", encoding="utf-8")
    (conf / "excludes").write_text(UBUNTU_EXCLUDES, encoding="utf-8")
    (conf / "docker-apt-speedup").write_text("force-unsafe-io\n", encoding="utf-8")

    result = run_lift(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "path-" not in (conf / "excludes").read_text(encoding="utf-8")
    assert (conf / "docker-apt-speedup").read_text(encoding="utf-8") == "force-unsafe-io\n"


def test_an_image_without_exclusions_passes_unchanged(tmp_path):
    conf = tmp_path / "dpkg.cfg.d"
    conf.mkdir()
    (conf / "docker-apt-speedup").write_text("force-unsafe-io\n", encoding="utf-8")

    result = run_lift(tmp_path)

    assert result.returncode == 0, result.stderr


def test_an_exclusion_outside_dpkg_cfg_d_fails_loudly(tmp_path):
    (tmp_path / "dpkg.cfg.d").mkdir()
    (tmp_path / "dpkg.cfg").write_text("path-exclude /usr/share/doc/*\n", encoding="utf-8")

    result = run_lift(tmp_path)

    assert result.returncode != 0
    assert "path-exclude still active" in result.stderr
