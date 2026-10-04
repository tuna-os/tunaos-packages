"""The deb and arch publishers get the same NEVER BREAK RDEPS gate as rpm.

Their publish shape differs — the whole index is regenerated in place,
not merged from a staged wave — so their gate is old-vs-new, but the
principle is identical (ebranch check-update, slopfest/sandogasa) and
so is the differential rule: only a dependency the OLD index resolved
may count as broken. A factory for every target gates every target's
publish path, or the gates themselves are the EL bias.
"""
from __future__ import annotations

import importlib.util
import io
import pathlib
import tarfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, filename=None):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / (filename or f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load("gate", "check-index-regression.py")
apt = load("apt_packages")
pacman = load("pacman_db")


def deb_index(text):
    return gate.deb_candidates(text)


OLD_PACKAGES = """\
Package: quickshell
Version: 0.1.0-1
Architecture: amd64

Package: dms
Version: 1.2.0-1
Architecture: all
Depends: quickshell (>= 0.1.0), libc6 (>= 2.34)
"""


def test_a_dependency_the_old_index_resolved_must_survive():
    """The wave replaces quickshell with a version below dms's floor."""
    new = OLD_PACKAGES.replace(
        "Version: 0.1.0-1", "Version: 0.0.9-1", 1)
    report = gate.regressions(deb_index(OLD_PACKAGES), deb_index(new), "deb")
    assert report["broken"] == {"dms": ["quickshell (>= 0.1.0)"]}


def test_a_dep_outside_the_view_is_never_noise():
    """libc6 comes from the distro archive the gate cannot see —
    unresolvable in both views, reported in neither."""
    report = gate.regressions(
        deb_index(OLD_PACKAGES), deb_index(OLD_PACKAGES), "deb")
    assert report["broken"] == {}


def test_new_distro_dependency_is_not_a_deb_index_regression():
    new = OLD_PACKAGES.replace(
        "Depends: quickshell (>= 0.1.0), libc6 (>= 2.34)",
        "Depends: quickshell (>= 0.1.0), libc6 (>= 2.34), libsecret-1-0")
    assert gate.regressions(
        deb_index(OLD_PACKAGES), deb_index(new), "deb")["broken"] == {}


def test_apt_candidate_is_the_highest_version_not_the_last_stanza():
    """pool/ accumulates every version; apt installs the highest.

    The old 0.1.0 stanza sits AFTER the 0.2.0 one here — a last-wins
    parse would judge against 0.1.0 and miss that the candidate is
    fine.
    """
    text = """\
Package: quickshell
Version: 0.2.0-1
Architecture: amd64

Package: quickshell
Version: 0.1.0-1
Architecture: amd64
"""
    index = gate.deb_candidates(text)
    assert index["packages"]["quickshell"]["evr"] == "0.2.0-1"


def test_an_or_group_is_satisfied_by_either_branch():
    old = """\
Package: qml-runtime
Version: 1-1
Architecture: amd64

Package: dms
Version: 1-1
Architecture: all
Depends: qml-runtime | qml-runtime-t64
"""
    # The new index renames the provider; the alternative still resolves.
    new = old.replace("Package: qml-runtime\n", "Package: qml-runtime-t64\n", 1)
    report = gate.regressions(deb_index(old), deb_index(new), "deb")
    assert report["broken"] == {}


def _db(entries) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for directory, desc in entries:
            data = desc.encode()
            info = tarfile.TarInfo(f"{directory}/desc")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _desc(name, version, depends=()):
    dep_block = "\n".join(depends)
    return (f"%NAME%\n{name}\n\n%VERSION%\n{version}\n\n%ARCH%\nx86_64\n\n"
            f"%BASE%\n{name}\n\n%DEPENDS%\n{dep_block}\n")


def test_the_pacman_leg_judges_with_alpm_ordering():
    """niri needs quickshell>=0.2; the new db downgrades to 0.2rc1 —
    OLDER under alpm's trailing-alpha rule, so this is a regression."""
    old = pacman.parse_db(_db([
        ("quickshell-0.2-1", _desc("quickshell", "0.2-1")),
        ("niri-25.02-1", _desc("niri", "25.02-1", ["quickshell>=0.2"])),
    ]))
    new = pacman.parse_db(_db([
        ("quickshell-0.2rc1-1", _desc("quickshell", "0.2rc1-1")),
        ("niri-25.02-1", _desc("niri", "25.02-1", ["quickshell>=0.2"])),
    ]))
    report = gate.regressions(old, new, "pacman")
    assert report["broken"] == {"niri": ["quickshell >= 0.2"]}


def test_new_distro_dependency_is_not_a_pacman_index_regression():
    old = pacman.parse_db(_db([
        ("roost-0.1.0-1", _desc("roost", "0.1.0-1", ["gtk4"])),
    ]))
    new = pacman.parse_db(_db([
        ("roost-0.1.0-2", _desc("roost", "0.1.0-2", ["gtk4", "libsecret"])),
    ]))
    assert gate.regressions(old, new, "pacman")["broken"] == {}


def test_new_dependency_on_a_removed_provider_still_fails():
    old = pacman.parse_db(_db([
        ("helper-1-1", _desc("helper", "1-1")),
        ("roost-0.1.0-1", _desc("roost", "0.1.0-1")),
    ]))
    new = pacman.parse_db(_db([
        ("roost-0.1.0-2", _desc("roost", "0.1.0-2", ["helper"])),
    ]))
    assert gate.regressions(old, new, "pacman")["broken"] == {"roost": ["helper"]}


def test_a_removed_package_is_reported_but_not_fatal():
    old = pacman.parse_db(_db([
        ("quickshell-0.2-1", _desc("quickshell", "0.2-1")),
        ("niri-25.02-1", _desc("niri", "25.02-1")),
    ]))
    new = pacman.parse_db(_db([
        ("niri-25.02-1", _desc("niri", "25.02-1")),
    ]))
    report = gate.regressions(old, new, "pacman")
    assert report["removed"] == ["quickshell"]
    assert report["broken"] == {}


def test_every_publish_path_carries_a_reverse_dep_gate():
    """The parity pin: rpm, deb, and pacman each gate their publish.

    A format added to the factory without a gate in its publish path
    should fail here, not be discovered in an incident review.
    """
    rpm_wave = (ROOT / "scripts" / "publish-rpm-wave.sh").read_text()
    assert "check-reverse-deps.py" in rpm_wave

    deb_flow = (ROOT / ".github" / "workflows"
                / "publish-tideforge-debs.yml").read_text()
    assert "check-index-regression.py --format deb" in deb_flow

    arch_flow = yaml.safe_load(
        (ROOT / ".github" / "workflows"
         / "publish-tideforge-arch.yml").read_text())
    publish_steps = arch_flow["jobs"]["publish"]["steps"]
    joined = "\n".join(step.get("run") or "" for step in publish_steps)
    assert "check-index-regression.py --format pacman" in joined
    names = [step.get("name") or "" for step in publish_steps]
    gate_at = next(i for i, n in enumerate(names) if "breaks the served" in n)
    sync_up = next(i for i, n in enumerate(names) if n == "Sync up")
    assert gate_at < sync_up, "the gate must run before anything syncs up"
