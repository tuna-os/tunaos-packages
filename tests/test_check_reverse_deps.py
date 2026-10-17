"""A wave that breaks what is already served must be refused, before upload.

Adapted from ebranch's check-update (slopfest/sandogasa). The scenarios
below are this repository's own incidents replayed as index arithmetic:
the glib2 Obsoletes hijack (run 32405815822) and the libnotify version
trap (#480). Both were publishes that changed the capability universe
with nothing asking what the change broke.
"""
from __future__ import annotations

import gzip
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rdeps = load("rdeps", ROOT / "scripts" / "check-reverse-deps.py")
vercmp = load("rpm_vercmp", ROOT / "scripts" / "rpm_vercmp.py")


def index(packages=None, provides=None, provides_evr=None):
    return {
        "packages": packages or {},
        "provides": {k: set(v) for k, v in (provides or {}).items()},
        "provides_evr": {k: set(v) for k, v in (provides_evr or {}).items()},
        "files": set(),
    }


def pkg(requires=(), requires_versioned=()):
    return {"arch": "x86_64", "evr": "1.0-1", "srpm": None,
            "requires": list(requires),
            "requires_versioned": list(requires_versioned)}


SERVED = index(
    packages={
        "libnotify": pkg(),
        "gnome-settings-daemon": pkg(
            requires=["libnotify"],
            requires_versioned=[("libnotify", ">=", "0:0.8.7")]),
        "gtkgreet": pkg(requires=["greetd"]),
        "greetd": pkg(),
    },
    provides={"libnotify": ["libnotify"], "greetd": ["greetd"]},
    provides_evr={"libnotify": ["0:0.8.7-1.el10"]},
)


def test_an_upgrade_that_keeps_reverse_deps_resolvable_passes():
    wave = index(
        packages={"libnotify": pkg()},
        provides={"libnotify": ["libnotify"]},
        provides_evr={"libnotify": ["0:0.8.8-1.el10"]},
    )
    report = rdeps.simulate(SERVED, wave, vercmp)
    assert report["replaced"] == ["libnotify"]
    assert report["broken_reverse_deps"] == {}
    assert report["wave_uninstallable"] == {}


def test_a_downgrade_below_a_served_constraint_is_refused():
    """The libnotify shape, run backwards.

    The served gnome-settings-daemon needs libnotify >= 0.8.7. A wave
    that replaces libnotify with 0.8.6 leaves it installed but
    unresolvable — the exact state #480 found the buildroots in.
    """
    wave = index(
        packages={"libnotify": pkg()},
        provides={"libnotify": ["libnotify"]},
        provides_evr={"libnotify": ["0:0.8.6-1.el10"]},
    )
    report = rdeps.simulate(SERVED, wave, vercmp)
    assert report["broken_reverse_deps"] == {
        "gnome-settings-daemon": ["libnotify >= 0:0.8.7"]}


def test_a_capability_only_the_replaced_package_provided_must_survive():
    """The new build of a package drops a provide the old one carried.

    A soname bump is the everyday version: the old greetd provided
    libgreetd.so.1, something served links it, and the replacement
    provides only libgreetd.so.2. dnf would discover this in users'
    transactions; the gate discovers it in the publisher.
    """
    served = index(
        packages={
            "greetd": pkg(),
            "gtkgreet": pkg(requires=["libgreetd.so.1"]),
        },
        provides={"greetd": ["greetd"], "libgreetd.so.1": ["greetd"]},
    )
    wave = index(
        packages={"greetd": pkg()},
        provides={"greetd": ["greetd"], "libgreetd.so.2": ["greetd"]},
    )
    report = rdeps.simulate(served, wave, vercmp)
    assert report["broken_reverse_deps"] == {"gtkgreet": ["libgreetd.so.1"]}


def test_a_dep_the_system_repos_satisfy_is_never_noise():
    """The gate is differential: the factory index is a thin layer.

    Every served package requires glibc from the target's system
    repositories, which the gate may not be able to see. Unresolvable
    before AND after the wave means "outside my view", not "broken" —
    otherwise the first run would report the whole repo.
    """
    served = index(
        packages={"gnome-shell": pkg(requires=["glibc", "libnotify"]),
                  "libnotify": pkg()},
        provides={"libnotify": ["libnotify"]},
    )
    wave = index(packages={"libnotify": pkg()},
                 provides={"libnotify": ["libnotify"]})
    report = rdeps.simulate(served, wave, vercmp)
    assert report["broken_reverse_deps"] == {}


def test_without_system_indexes_the_wave_check_stays_differential():
    """pulseaudio was never in view; the gate cannot call it missing."""
    wave = index(
        packages={"xfce4-pulseaudio-plugin": pkg(requires=["pulseaudio"])},
    )
    report = rdeps.simulate(SERVED, wave, vercmp)
    assert report["wave_uninstallable"] == {}


def test_with_system_indexes_the_waves_own_packages_must_resolve():
    """The complete view: --system-index makes absence meaningful.

    This is the #480 xfce shape at publish time — pulseaudio in no
    system repo and no factory prefix, so the wave package can never
    install and the wave is refused.
    """
    system = index(packages={"glibc": pkg()},
                   provides={"glibc": ["glibc"]})
    wave = index(
        packages={"xfce4-pulseaudio-plugin": pkg(
            requires=["glibc", "pulseaudio"])},
    )
    report = rdeps.simulate(SERVED, wave, vercmp, system=system)
    assert report["wave_uninstallable"] == {
        "xfce4-pulseaudio-plugin": ["pulseaudio"]}


def test_a_wave_package_losing_a_previously_served_dep_is_caught():
    """Even without system indexes: gtkgreet's greetd WAS in view.

    The wave ships gtkgreet while simultaneously replacing greetd with
    a build that drops the capability — judgeable entirely within the
    factory's own view, so it must be caught without --system-index.
    """
    served = index(
        packages={"greetd": pkg()},
        provides={"greetd": ["greetd"], "libgreetd.so.1": ["greetd"]},
    )
    wave = index(
        packages={"gtkgreet": pkg(requires=["libgreetd.so.1"]),
                  "greetd": pkg()},
        provides={"greetd": ["greetd"], "libgreetd.so.2": ["greetd"]},
    )
    report = rdeps.simulate(served, wave, vercmp)
    assert report["wave_uninstallable"] == {"gtkgreet": ["libgreetd.so.1"]}


def test_rich_and_rpmlib_requires_are_not_judged():
    wave = index(
        packages={"tool": pkg(requires=["(a or b)", "rpmlib(PayloadIsZstd)"])},
    )
    report = rdeps.simulate(SERVED, wave, vercmp)
    assert report["wave_uninstallable"] == {}


def test_an_unversioned_provider_cannot_fail_a_versioned_require():
    """caps_evr absent -> not judgeable -> not a finding."""
    served = index(
        packages={
            "consumer": pkg(requires=["libfoo"],
                            requires_versioned=[("libfoo", ">=", "0:2.0")]),
            "libfoo": pkg(),
        },
        provides={"libfoo": ["libfoo"]},  # provided with no version anywhere
    )
    wave = index(packages={"other": pkg()})
    report = rdeps.simulate(served, wave, vercmp)
    assert report["broken_reverse_deps"] == {}


def _write_wave_repo(tmp_path):
    primary = b"""<?xml version="1.0"?>
<metadata xmlns="http://linux.duke.edu/metadata/common"
          xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="1">
  <package type="rpm">
    <name>glib2</name><arch>x86_64</arch>
    <version epoch="0" ver="2.87.3" rel="1.el10"/>
    <format>
      <rpm:provides><rpm:entry name="glib2" flags="EQ" epoch="0"
        ver="2.87.3" rel="1.el10"/></rpm:provides>
      <rpm:obsoletes><rpm:entry name="glib2" flags="LT" epoch="0"
        ver="2.87.3"/></rpm:obsoletes>
    </format>
  </package>
</metadata>"""
    repodata = tmp_path / "wave" / "repodata"
    repodata.mkdir(parents=True)
    (repodata / "primary.xml.gz").write_bytes(gzip.compress(primary))
    (repodata / "repomd.xml").write_text("""<?xml version="1.0"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <revision>1</revision>
  <data type="primary">
    <location href="repodata/primary.xml.gz"/>
    <checksum type="sha256">x</checksum>
  </data>
</repomd>""")
    return tmp_path / "wave"


def test_a_local_staged_repo_is_readable(tmp_path):
    """The gate reads the same repodata createrepo_c just wrote."""
    wave_repo = _write_wave_repo(tmp_path)
    blob = rdeps.local_primary(wave_repo)
    parsed = rdeps.gap_engine.parse_primary(blob)
    assert list(parsed["packages"]) == ["glib2"]


def test_an_obsoletes_against_a_served_name_is_reported(tmp_path):
    """The glib2 hijack is at minimum never silent again.

    `Obsoletes: glib2 < 2.87.3` REPLACED AppStream's glib2 in every
    transaction regardless of repo priority. An intentional rename uses
    the same mechanism, so this is informational — but it must be in
    the report, because silence is how it shipped the first time.
    """
    wave_repo = _write_wave_repo(tmp_path)
    blob = rdeps.local_primary(wave_repo)
    obsoletes = rdeps.parse_obsoletes(blob)
    assert obsoletes == {"glib2": ["glib2"]}
