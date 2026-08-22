"""The deb buildroot must not inherit whatever apt components the base ships.

Ubuntu keeps a large share of Debian-synced packages in `universe`.
quickshell needs two of them (libcli11-dev, libcpptrace-dev), and the ubuntu
cell reported `libcpptrace-dev but it is not installable ... [no choices]`
while the same packages installed cleanly on Debian sid in the same run
(32541649752). Pinning the components makes the buildroot independent of the
base image's defaults; on Debian there is no ubuntu.sources and no such
component, so it is a no-op.

The diagnostic exists because `apt-get build-dep` reports an unsatisfiable
dependency as a cascade: one genuinely missing package produces a dozen
"but it is not going to be installed" lines for packages that are fine. That
cost a whole run to interpret, twice. Printing each declared build-dep's
candidate version first names the real one in the same job that fails.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-package-factory-cell.sh"


def deb_block() -> str:
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index("  deb)")
    return text[start:text.index("  pkg.tar.zst)", start)]


def test_the_deb_buildroot_pins_apt_components():
    assert "Components: main restricted universe multiverse" in deb_block()


def test_components_are_pinned_before_apt_update():
    """Rewriting sources after `apt-get update` leaves the old index in place,
    so the new component is invisible for this build."""
    block = deb_block()
    assert block.index("Components:") < block.index("apt-get update")


def test_it_edits_only_ubuntus_sources_file():
    """Debian has no universe; guarding on ubuntu.sources keeps this a no-op
    there rather than corrupting a Debian buildroot's sources."""
    block = deb_block()
    assert "/etc/apt/sources.list.d/ubuntu.sources" in block
    assert "if [ -f /etc/apt/sources.list.d/ubuntu.sources ]" in block


def test_build_dep_availability_is_printed_before_resolution():
    block = deb_block()
    assert "build-dependency availability" in block
    # Match the invocation, not the prose: "apt-get build-dep" also appears in
    # the comment explaining why this probe exists, and that comment sits
    # ABOVE the probe.
    assert block.index("build-dependency availability") < block.index("apt-get build-dep -y")


def test_a_missing_dependency_is_labelled_not_available():
    """Without an explicit label a missing candidate prints as an empty column
    and reads like a formatting glitch rather than the answer."""
    assert "NOT AVAILABLE" in deb_block()


def test_debhelper_compat_is_excluded_from_the_probe():
    """It is a build-profile token, not an installable package; probing it
    would always print NOT AVAILABLE and train readers to ignore the column."""
    assert "debhelper-compat" in deb_block()


def test_the_deb_container_receives_the_published_index():
    """It did not, and that was the whole bug. PUBLISHED_INDEX was passed to
    the rpm container only, so any deb recipe whose BuildRequires are
    themselves factory-built was unsatisfiable by construction -- quickshell
    needs libcpptrace-dev, which Ubuntu does not ship at all."""
    assert '--env PUBLISHED_INDEX' in deb_block()


def test_each_index_becomes_an_apt_source():
    block = deb_block()
    assert "sources.list.d/tunaos-published-" in block
    assert "deb [trusted=yes]" in block


def test_the_published_repo_is_pinned_below_the_distro_default():
    """A buildroot must take every package it can from its own archive. The
    rpm path carries priority=999 for this reason after a served-repo package
    outranked and replaced a base one. apt's default is 500, so the pin has to
    be strictly lower to be a gap-filler rather than an override."""
    import re
    block = deb_block()
    priorities = [int(m) for m in re.findall(r"Pin-Priority: (\d+)", block)]
    assert priorities, block
    assert all(p < 500 for p in priorities), priorities


def test_the_sources_are_written_before_the_refreshing_apt_update():
    """An apt source added after the last `apt-get update` is invisible for
    this build -- the index is never fetched.

    This asserted `< block.index("apt-get update")`, the FIRST one, back when
    there was only one. There are two now: the buildroot has to update once to
    install ca-certificates before it can fetch an HTTPS index at all, then
    again to read the indexes themselves. Against the first update the
    invariant is now false, and the correct one is about the last.
    """
    block = deb_block()
    assert block.index("sources.list.d/tunaos-published-") < block.rindex("apt-get update")


def test_the_pin_targets_the_index_host_not_a_hardcoded_one():
    """published_index is per-target and may move; a literal hostname would
    silently stop pinning and let the repo outrank the distro archive."""
    assert "published_host" in deb_block()


# ---------------------------------------------- the indexes are served over TLS


def test_ca_certificates_is_installed_before_the_published_indexes_are_added():
    """The published indexes are HTTPS, so a buildroot with no CA bundle
    cannot fetch them -- and apt calls that a WARNING and exits 0:

        W: Failed to fetch https://repo.tunaos.org/tideforge/ubuntu/./InRelease
           SSL connection failed: certificate verify failed

    The cell then failed a hundred lines later at `libcpptrace-dev
    NOT AVAILABLE`, which points at the recipe rather than at the buildroot,
    and cost a full publish-and-rebuild round to see. The Ubuntu archive
    itself is plain HTTP, which is why every other dependency resolved and
    hid this.
    """
    block = deb_block()
    assert block.index("ca-certificates") < block.index("tunaos-published")


def test_the_index_is_refreshed_after_being_added():
    """Writing a source without a following update leaves it unread, which
    would make the reordering above pointless."""
    block = deb_block()
    assert block.index("tunaos-published") < block.rindex("apt-get update")


def test_an_unfetchable_published_index_fails_the_cell():
    """apt exits 0 when a source cannot be fetched, so silence is not proof.
    A declared index that cannot be read is a buildroot fault and has to be
    reported as one, not as an unexplained missing dependency later."""
    block = deb_block()
    assert "Failed to fetch" in block
    assert block.index("Failed to fetch") < block.index("apt-get build-dep -y")
