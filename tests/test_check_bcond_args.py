"""%check's BuildRequires: are dropped with %check, in scripts/build-chain.sh.

Without --with-checks the build passes --nocheck, so %check never runs -- but
until this was added its BuildRequires: were still installed.  Fedora guards
them behind a bcond, and for a bootstrap buildroot the guarded lines are the
expensive ones:

    python-flit-core    %bcond tests -> python3-pytest, python3-testpath
    python-poetry-core  %bcond tests -> python3-pytest-mock, python3-virtualenv,
                                        python3-build, python3-tomli-w, ...

Those exist only in Rawhide, built for Python 3.15, while Hummingbird is on
3.14, so dnf5 could not resolve the buildroot at all and the build died before
rpmbuild started (run 31262874931, both packages of tier bootstrap-00):

    package python3-testpath-0.6.0-27.fc45.noarch from fedora requires
    python(abi) = 3.15, but none of the providers can be installed

The arguments have to reach two places for that to hold: the SRPM build,
because that header is what mock's `dnf builddep` reads, and mock itself, so
the dynamic-BuildRequires pass inside the chroot agrees with the header.  A
fix present in only one of the two looks right and changes nothing.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-chain.sh"


def bcond_block() -> str:
    """The `SRPM_BCOND_ARGS`/`MOCK_BCOND_ARGS` assignment, lifted verbatim."""
    text = SCRIPT.read_text()
    match = re.search(r"^SRPM_BCOND_ARGS=\(\).*?^fi$", text, re.S | re.M)
    assert match, "the bcond block is gone from build-chain.sh"
    return match.group(0)

def backend_sources() -> str:
    return "\n".join(
        (ROOT / path).read_text()
        for path in (
            "scripts/lib/build-chain/podman.sh",
            "scripts/lib/build-chain/mock.sh",
        )
    )



def evaluate(with_checks: bool) -> tuple[list[str], str]:
    """Run the real block for a given --with-checks and report what it built."""
    script = f"""
set -euo pipefail
WITH_CHECKS={str(with_checks).lower()}
{bcond_block()}
printf 'SRPM:%s\\n' "${{SRPM_BCOND_ARGS[*]-}}"
printf 'MOCK:%s\\n' "${{MOCK_BCOND_ARGS}}"
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    srpm, mock = proc.stdout.splitlines()
    return srpm[len("SRPM:") :].split(), mock[len("MOCK:") :].split()


def test_checks_disabled_turns_off_both_bconds():
    srpm, mock = evaluate(with_checks=False)
    assert srpm == ["--without", "tests", "--without", "check"]
    assert mock == ["--without=tests", "--without=check"]


def test_with_checks_leaves_every_bcond_alone():
    """--with-checks runs %check, so it must keep %check's BuildRequires."""
    srpm, mock = evaluate(with_checks=True)
    assert srpm == []
    assert mock == []


def test_empty_arrays_survive_set_u():
    """`set -u` plus an empty array is the classic way this breaks silently."""
    script = f"""
set -euo pipefail
WITH_CHECKS=true
{bcond_block()}
: "${{SRPM_BCOND_ARGS[@]}}" "${{MOCK_BCOND_ARGS}}"
echo ok
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_the_srpm_build_carries_the_bconds():
    """mock's builddep reads the SRPM header, so the SRPM must be built with them."""
    text = backend_sources()
    srpm_builds = re.findall(
        r"rpmbuild -bs .*?(?=\n\n)", text, re.S
    )
    assert srpm_builds, "no SRPM build found in build-chain.sh"
    for invocation in srpm_builds:
        assert '"${SRPM_BCOND_ARGS[@]}"' in invocation, (
            "an SRPM build without the bconds bakes the test BuildRequires "
            f"back into the header:\n{invocation}"
        )


def test_every_mock_invocation_carries_the_bconds():
    """And the chroot has to agree with the header it was handed."""
    text = backend_sources()
    invocations = [
        line for line in text.splitlines() if re.search(r"\$\{mock_check_flag\}", line)
    ]
    assert invocations, "no mock invocation found in build-chain.sh"
    for line in invocations:
        assert "${MOCK_BCOND_ARGS}" in line, (
            f"mock is told --nocheck but not the matching bconds:\n{line}"
        )
