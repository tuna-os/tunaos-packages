"""The verify step must install what the chain ships, and only that.

gnome51-el10-aarch64 at 8086e03 reported "All packages built successfully!"
with 55 packages and a clean lint, then failed in the verify step -- the next
layer down, and one no gnome cell had reached before. Two independent defects:

1. DUPLICATE NEVRAs. The step ran `dnf install /factory-repo/*.rpm`, passing
   every built FILE. A build-chain tier list may build the same recipe twice
   on purpose:

       tier 2 glib2-bootstrap  src/gnome-51/glib2
       tier 6 glib2-full       src/gnome-51/glib2
       tier 5 gi-bootstrap     src/gnome-51/gobject-introspection
       tier 7 gi-full          src/gnome-51/gobject-introspection

   so /artifacts holds two EVRs of each, and asking dnf for both at once is
   unsatisfiable by construction:

       cannot install both glib2-2.88.0-4.el10.aarch64 from @commandline
       and glib2-2.88.0-1.el10.aarch64 from @commandline

   Installing by NAME resolves each to the newest EVR in the factory repo --
   the build the chain means to ship -- while rpm -q and rpm -V still cover
   every name, so coverage is unchanged. This is why only gnome cells hit it:
   xfce and fprintd have no bootstrap pass, which is also why the step looked
   correct for as long as it did.

2. tigervnc. avahi-ui-tools carried a hard `Requires: tigervnc`, and EL10 has
   no tigervnc in BaseOS, AppStream, CRB or EPEL, on either arch -- measured
   against the repository metadata, where EPEL 10 lists 25573 names and none
   of them is tigervnc. So the subpackage could not be installed on this
   target at all:

       nothing provides tigervnc needed by avahi-ui-tools-0.9~rc2-8.el10.aarch64

   Recommends is the fix rather than deletion: the package installs where
   tigervnc is absent and still pulls it in where it exists. Only bvnc needs
   it; bssh and the rest of the subpackage do not.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts" / "verify-package-factory-cell.sh"


def test_the_build_chain_verify_installs_by_name_not_by_file():
    text = VERIFY.read_text(encoding="utf-8")
    # The install must name the resolved package list...
    assert '"${names[@]}"\n' in text
    # ...and must not hand dnf a glob of every built file.
    assert "/factory-repo/*.rpm\n" not in text, (
        "installing every file re-introduces the duplicate-NEVRA failure"
    )
    # names must be computed before it is used to install.
    computed = text.index("mapfile -t names")
    installed = text.index("dnf -y install --nogpgcheck --repofrompath factory")
    assert computed < installed, "names is used before it is set"


def test_every_name_is_still_queried_and_verified():
    """Installing by name must not quietly shrink what gets checked."""
    text = VERIFY.read_text(encoding="utf-8")
    assert 'rpm -q "${names[@]}"' in text
    assert 'rpm -V --nouser --nogroup "${names[@]}"' in text


def test_rpm_verify_excludes_only_what_a_container_cannot_judge():
    """Two classes, and each is excluded for a reason, not for convenience.

    Both took all four gnome cells red in run 32646102181 -- after a clean
    build, a clean lint and a clean install:

        .....UG..    /var/lib/avahi-autoipd
        .M.......  g /var/lib/selinux/minimum/active/policy.linked

    A %ghost path (the `g`) is one rpm knows the name of but ships no content
    for; a scriptlet creates it and the header attributes are placeholders.
    An account like avahi-autoipd is created by sysusers, which a container
    without systemd never runs, so rpm chowns to root and reports U and G.

    Neither is a statement about the package. What IS still checked -- digest,
    size, mode, link target, capabilities -- is what catches a corrupt or
    mis-packaged payload, and that is what this step exists for.
    """
    text = VERIFY.read_text(encoding="utf-8")
    assert "--nouser --nogroup" in text
    # Ghosts are dropped by their marker column, not by path.
    assert 'grep -vE "^[.?SM5DLUGTPa]{8,9} +g "' in text
    for kept in ("--nofiledigest", "--nosize", "--nomode", "--nolinkto", "--nocaps"):
        assert kept not in text, (
            f"{kept} would disable a check that detects a real payload defect"
        )


def test_a_surviving_rpm_verify_difference_still_fails_the_cell():
    """Filtering must not become ignoring.

    A grep that drops ghost lines and then discards the rest would turn this
    step into a no-op that reports success -- the worst outcome, because the
    step would keep looking like a gate.
    """
    text = VERIFY.read_text(encoding="utf-8")
    filtered = text.index("rpm-verify.real")
    fail = text.index('if [ -s /tmp/rpm-verify.real ]; then')
    assert filtered < fail
    tail = text[fail:fail + 400]
    assert "exit 1" in tail, "surviving differences must fail the cell"
    assert "cat /tmp/rpm-verify.real" in tail, "and must be printed, not swallowed"


def test_a_recipe_really_is_built_twice_in_the_gnome_chains():
    """The premise of the fix, asserted rather than assumed. If the bootstrap
    passes ever go away this test should be removed, not worked around."""
    for order in ("build-order.yml", "build-order-gnome51.yml"):
        data = yaml.safe_load((ROOT / order).read_text(encoding="utf-8"))
        paths = [
            pkg["path"]
            for tier in data.get("tiers", [])
            for pkg in (tier.get("packages") or [])
            if pkg.get("path")
        ]
        repeated = {path for path, n in Counter(paths).items() if n > 1}
        assert repeated, f"{order}: expected at least one recipe built twice"
        assert any("glib2" in p for p in repeated), (order, repeated)


def test_avahi_does_not_hard_require_something_el10_lacks():
    text = (ROOT / "src" / "deps" / "avahi" / "avahi.spec").read_text(encoding="utf-8")
    assert re.search(r"^Recommends:\s+tigervnc\s*$", text, re.M), text[:0]
    assert not re.search(r"^Requires:\s+tigervnc\s*$", text, re.M), (
        "a hard Requires on tigervnc makes avahi-ui-tools uninstallable on EL10"
    )


def _newest_by_source_block() -> str:
    """The real selection code, lifted out of the container script."""
    text = VERIFY.read_text(encoding="utf-8")
    start = text.index("    declare -A newest_srpm")
    end = text.index("verifying ${#names[@]} package name(s)")
    end = text.index("\n", end) + 1
    return text[start:end]


def test_only_the_newest_build_of_each_source_is_installed():
    """Installing the newest by NAME was not enough.

    A subpackage produced ONLY by the superseded bootstrap pass has its own
    newest version in that older build, and carries an exact `= EVR` dep on a
    sibling whose newest is the newer build:

        cannot install both gobject-introspection-debuginfo-1.86.0-2 and -1
          - gobject-introspection-devel-debuginfo-1.86.0-1 requires
            gobject-introspection-debuginfo(aarch-64) = 1.86.0-1.el10

    Newest by SOURCE drops the bootstrap pass whole, so no orphan subpackage
    survives to demand a sibling that is no longer the newest.

    This runs the script's own code with a stubbed rpm, so it tests the
    shipped algorithm rather than a copy of it.
    """
    import subprocess
    import textwrap

    fixture = textwrap.dedent("""\
        glib2-2.88.0-1.el10.src.rpm|glib2
        glib2-2.88.0-1.el10.src.rpm|glib2-devel
        glib2-2.88.0-4.el10.src.rpm|glib2
        glib2-2.88.0-4.el10.src.rpm|glib2-devel
        gobject-introspection-1.86.0-1.el10.src.rpm|gobject-introspection
        gobject-introspection-1.86.0-1.el10.src.rpm|gobject-introspection-devel-debuginfo
        gobject-introspection-1.86.0-2.el10.src.rpm|gobject-introspection
        pango-1.55.0-1.el10.src.rpm|pango
        """)
    script = (
        "set -euo pipefail\n"
        f"rpm() {{ cat <<'FIXTURE'\n{fixture}FIXTURE\n}}\n"
        + _newest_by_source_block()
        + 'printf "%s\\n" "${names[@]}"\n'
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    names = [line for line in out.stdout.splitlines() if line and not line.startswith("==>")]

    assert "gobject-introspection" in names
    assert "glib2" in names and "glib2-devel" in names and "pango" in names
    # The whole point: the bootstrap-only subpackage must not be installed.
    assert "gobject-introspection-devel-debuginfo" not in names, names


def test_the_container_block_carries_no_apostrophe():
    """The verify body is a single-quoted `bash -lc` string, so one apostrophe
    anywhere inside ends the string and the shell fails on a later line that
    looks innocent. Adding a comment containing "rpm's" broke it exactly that
    way, and the reported syntax error pointed at an unrelated comment 40
    lines further down.
    """
    text = VERIFY.read_text(encoding="utf-8")
    start = text.index('"${IMAGE:?}" -lc \'')
    end = text.index("\n  '\n", start)
    body = text[start:end]
    assert "'" not in body[len('"${IMAGE:?}" -lc \''):], (
        "an apostrophe inside the single-quoted container body ends it early"
    )
