"""The build-dep probe must list build-dependencies, nothing else, and never
fail the cell.

The probe prints each declared build-dep's apt candidate so an unsatisfiable
one is named in the job that fails, instead of being buried in `apt-get
build-dep`'s cascade of "but it is not going to be installed" lines. It has
had two defects, and both are pinned here.

1. It read too much. The extraction ran from `Build-Depends:` to the next
   BLANK line, but in deb822 the source stanza continues past Build-Depends
   into the fields after it -- the blank line does not come until the first
   binary package stanza. So `Standards-Version: 4.6.2` and
   `Rules-Requires-Root: no` were fed to `apt-cache policy` as package names
   and printed NOT AVAILABLE: two guaranteed false alarms in the one column
   whose job is making a real NOT AVAILABLE stand out.

2. Fixing (1) broke every dependency-light recipe. `grep` exits 1 when it
   matches nothing, and a recipe whose only Build-Depends is debhelper-compat
   leaves it with nothing to print. Under `set -o pipefail` that failed the
   cell immediately after printing the probe header. It took out all four
   wayland-protocols deb cells -- and wayland-protocols is a planner canary,
   so it rides along on every infra change. Defect (1) had been masking it:
   the over-read always gave grep at least Standards-Version to print.

These tests EXECUTE the probe lifted out of the runner, under the same
`set -euo pipefail` the cell uses and with a stub apt-cache, so they measure
behaviour. A test that only matched the script's text would have passed
through both defects.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-package-factory-cell.sh"

STUB_CANDIDATES = {
    "cmake": "4.2.3-2ubuntu2",
    "libzstd-dev": "1.5.7+dfsg-1",
    "libdwarf-dev": "20250521-1",
    "ninja-build": "1.13.2-1",
    "qt6-base-dev": "6.10.2+dfsg-7",
    "libwayland-dev": "1.24.0-1",
}


def probe_source() -> str:
    """The probe block, lifted verbatim from the runner.

    Derived from the script rather than duplicated, so these tests run
    against whatever ships. Bounded by the header it prints and the
    resolution step it precedes.
    """
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index('echo "==> build-dependency availability"')
    end = text.index("apt-get build-dep -y", start)
    return text[start:end]


def run_probe(control: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the probe the way the cell does: set -euo pipefail, stub apt-cache."""
    workdir = tmp_path / "src"
    (workdir / "debian").mkdir(parents=True)
    (workdir / "debian" / "control").write_text(control, encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "apt-cache"
    cases = "\n".join(
        f'    {name}) echo "  Candidate: {ver}" ;;' for name, ver in STUB_CANDIDATES.items()
    )
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'case "$2" in\n'
        f"{cases}\n"
        # A package apt has never heard of produces NO output at all -- that is
        # what makes the probe print NOT AVAILABLE, and it is what the real
        # ubuntu cell showed for libcpptrace-dev. `Candidate: (none)` is a
        # different state (known package, no installable candidate).
        '    *) ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + probe_source()],
        cwd=workdir,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


def listed(result: subprocess.CompletedProcess) -> list[str]:
    """The package names in the probe's first column, header excluded."""
    out = []
    for line in result.stdout.splitlines():
        if not line.strip() or line.startswith("==>") or line.startswith("("):
            continue
        out.append(line.split()[0])
    return out


CONTROL = """Source: cpptrace-devel
Section: libdevel
Priority: optional
Maintainer: TunaOS <ci@tunaos.org>
Build-Depends: debhelper-compat (= 13),
               cmake,
               libzstd-dev,
               libdwarf-dev
Standards-Version: 4.6.2
Rules-Requires-Root: no

Package: libcpptrace-dev
Architecture: any
Depends: ${misc:Depends}
Description: stacktrace library
"""

# wayland-protocols and every other `build_system: data` recipe render this.
NO_DEPS_CONTROL = """Source: wayland-protocols
Section: devel
Priority: optional
Maintainer: TunaOS <ci@tunaos.org>
Build-Depends: debhelper-compat (= 13)
Standards-Version: 4.6.2
Rules-Requires-Root: no

Package: wayland-protocols
Architecture: all
Description: protocol definitions
"""


# --------------------------------------------------- defect 2: it must not fail


def test_a_recipe_with_no_real_build_deps_does_not_fail_the_cell(tmp_path):
    """The wayland-protocols regression, reproduced exactly.

    grep exits 1 on no match and the cell runs under `set -o pipefail`, so
    this died right after printing the header -- taking out all four
    wayland-protocols deb cells, on a canary package that is planned into
    every infra change.
    """
    result = run_probe(NO_DEPS_CONTROL, tmp_path)
    assert result.returncode == 0, result.stderr


def test_an_empty_dependency_list_still_says_something(tmp_path):
    """Printing a bare header and nothing else reads like the probe broke."""
    result = run_probe(NO_DEPS_CONTROL, tmp_path)
    assert "none declared beyond debhelper-compat" in result.stdout


def test_the_normal_case_still_exits_clean(tmp_path):
    result = run_probe(CONTROL, tmp_path)
    assert result.returncode == 0, result.stderr


# ------------------------------------------------ defect 1: it must not overread


def test_it_lists_exactly_the_build_dependencies(tmp_path):
    assert listed(run_probe(CONTROL, tmp_path)) == ["cmake", "libzstd-dev", "libdwarf-dev"]


def test_the_fields_after_build_depends_are_not_probed(tmp_path):
    got = listed(run_probe(CONTROL, tmp_path))
    assert not [d for d in got if "Standards-Version" in d or "Rules-Requires-Root" in d]


def test_no_probed_name_is_a_deb822_field(tmp_path):
    """Stated as a property, so a control file carrying some other trailing
    field (Homepage, Vcs-Git, Testsuite) cannot reintroduce the same noise."""
    for dep in listed(run_probe(CONTROL, tmp_path)):
        assert not re.match(r"^[A-Za-z][A-Za-z0-9-]*:", dep), dep


def test_a_binary_stanza_depends_is_never_read(tmp_path):
    """Build-Depends can be the last field of the source stanza. The binary
    stanza's Depends is a runtime dependency and must not be probed."""
    control = (
        "Source: x\nStandards-Version: 4.6.2\n"
        "Build-Depends: cmake,\n               ninja-build\n"
        "\nPackage: y\nDepends: runtime-only-not-a-builddep\n"
    )
    assert listed(run_probe(control, tmp_path)) == ["cmake", "ninja-build"]


def test_a_single_line_build_depends_still_works(tmp_path):
    control = (
        "Source: x\n"
        "Build-Depends: debhelper-compat (= 13), cmake, ninja-build\n"
        "Standards-Version: 4.6.2\n\nPackage: y\n"
    )
    assert listed(run_probe(control, tmp_path)) == ["cmake", "ninja-build"]


def test_version_constraints_are_stripped(tmp_path):
    """`apt-cache policy 'qt6-base-dev (>= 6.8)'` finds nothing; the probe must
    ask about the package, not the dependency expression."""
    control = (
        "Source: x\n"
        "Build-Depends: qt6-base-dev (>= 6.8), libwayland-dev (>= 1.41)\n"
        "\nPackage: y\n"
    )
    assert listed(run_probe(control, tmp_path)) == ["qt6-base-dev", "libwayland-dev"]


# ------------------------------------------------------- and it still diagnoses


def test_a_missing_candidate_is_labelled_not_available(tmp_path):
    """The whole reason the probe exists: name the unsatisfiable dep here,
    rather than leaving it buried in apt-get build-dep's cascade."""
    control = "Source: x\nBuild-Depends: cmake, libcpptrace-dev\n\nPackage: y\n"
    result = run_probe(control, tmp_path)
    assert "NOT AVAILABLE" in result.stdout
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("libcpptrace-dev"))
    assert "NOT AVAILABLE" in line


def test_debhelper_compat_is_excluded(tmp_path):
    assert "debhelper-compat" not in listed(run_probe(CONTROL, tmp_path))
