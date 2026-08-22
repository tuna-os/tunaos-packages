"""Every format's verify container must be able to see the published repo.

A clean-install verify resolves the built package's OWN declared dependencies.
When one of those is itself factory-built and published, the verify container
has to have the published index or the install cannot succeed -- against a
package that is built, published and served.

Both RPM branches (zypper and dnf) had it. The deb branch did not, and that is
what stopped quickshell's ubuntu cell after it finally compiled: it links
libcpptrace.so.1, dpkg-shlibdeps resolved that to libcpptrace-dev, and the
install died with

    quickshell:amd64 Depends libcpptrace-dev (>= 1.0.4)
      but none of the choices are installable: [no choices]

while libcpptrace-dev 1.0.4-1 was live in the served ubuntu index.

The resolver call also used to live INSIDE the rpm branch, so referencing it
from the deb branch was an unbound variable under `set -u`. It is hoisted above
the case now, and pinned here.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts" / "verify-package-factory-cell.sh"


def script() -> str:
    return VERIFY.read_text(encoding="utf-8")


def branch(name: str) -> str:
    text = script()
    start = text.index(f"\n  {name})")
    following = [m.start() for m in re.finditer(r"\n  (rpm|deb|pkg\.tar\.zst|\*)\)", text)
                 if m.start() > start]
    return text[start:following[0]] if following else text[start:]


def test_the_index_is_resolved_once_for_every_format():
    """Assigned above the case, not inside one branch. Inside rpm) it was out
    of scope for deb) -- an unbound variable under set -u, not merely absent."""
    text = script()
    assert text.index("published_index=") < text.index("case ${FORMAT:?} in")


def test_the_deb_verify_container_receives_the_published_index():
    assert "PUBLISHED_INDEX=" in branch("deb")


def test_every_format_branch_passes_the_index_to_its_container():
    """Stated over all branches rather than just deb: the asymmetry is exactly
    what went unnoticed, so a third format cannot be added without it."""
    for name in ("rpm", "deb"):
        assert "PUBLISHED_INDEX=" in branch(name), name


def test_the_deb_verify_pins_the_published_repo_below_the_artifact_under_test():
    """The local repo is pinned 1001 so the cell's OWN build is what gets
    verified. The published index must sit below apt's default 500 as well, so
    it can only ever fill a gap -- never substitute a published build for the
    artifact under test, which would make the gate verify the wrong thing."""
    deb = branch("deb")
    priorities = [int(p) for p in re.findall(r"Pin-Priority: (\d+)", deb)]
    assert 1001 in priorities, priorities
    published = [p for p in priorities if p != 1001]
    assert published and all(p < 500 for p in published), priorities


def test_ca_certificates_is_installed_before_the_published_sources_are_added():
    """The indexes are HTTPS. Adding them before a CA bundle exists makes apt
    skip them with a warning and exit 0 -- the failure then surfaces as an
    unexplained missing package much later. Same ordering the build container
    had to learn."""
    deb = branch("deb")
    assert deb.index("ca-certificates") < deb.index("tunaos-published")


def test_an_unfetchable_index_fails_the_deb_verify_loudly():
    """apt exits 0 on a source it could not fetch, so silence is not proof."""
    deb = branch("deb")
    assert "Failed to fetch" in deb
    assert deb.index("Failed to fetch") < deb.index('apt-get install -y "$INSTALL_NAME"')
