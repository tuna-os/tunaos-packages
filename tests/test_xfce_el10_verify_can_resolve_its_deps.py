"""The xfce-el10 clean-install verify must be able to resolve its own deps.

It could not, on EITHER arch (#482). The aarch64 cell surfaced it, but the
cause was arch-independent: gtkgreet Requires greetd and
xfce4-pulseaudio-plugin Required pulseaudio, and neither name existed in any
repository the build-chain verify enables.

Measured 2026-08-22 from published metadata, across all eight:

    EPEL 10 x86_64 (25729 pkgs) / aarch64 (25572)   greetd no  pulseaudio no
    CS10 AppStream x86_64 (4726) / aarch64 (4651)   greetd no  pulseaudio no
    CS10 BaseOS   x86_64 / aarch64                  greetd no  pulseaudio no
    CS10 CRB      x86_64 / aarch64                  greetd no  pulseaudio no

Two different problems wearing one error message:

  greetd     IS ours and IS published, live at rpm/el10/{arch}. The
             build-chain branch just never passed PUBLISHED_INDEX, unlike
             both tideforge branches, so it could not see anything this
             factory publishes.
  pulseaudio does not exist on EL10 at all. pipewire-pulseaudio OBSOLETES it
             and provides pulseaudio-daemon -- but not the bare name.
"""
from __future__ import annotations

import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts" / "verify-package-factory-cell.sh"
SPEC = Path(glob.glob(str(ROOT / "src/xfce-wayland/xfce4-pulseaudio-plugin/*.spec"))[0])


def verify() -> str:
    return VERIFY.read_text(encoding="utf-8")


def build_chain_branch() -> str:
    text = verify()
    start = text.index("if [[ ${ENGINE:?} == build-chain ]]; then")
    return text[start:text.index("\ncase ${FORMAT:?} in", start)]


def test_published_index_is_resolved_before_the_engine_branch():
    """It used to be assigned BELOW the build-chain branch, so that branch read
    an empty value. The same bug, one level up, previously left the deb branch
    with no published repo either."""
    text = verify()
    assert text.index("published_index=\"$(python3 scripts/published_index.py") < text.index(
        "if [[ ${ENGINE:?} == build-chain ]]; then"
    )


def test_the_build_chain_container_receives_the_index():
    assert '--env PUBLISHED_INDEX="$published_index"' in build_chain_branch()


def test_the_build_chain_install_enables_every_published_repo():
    branch = build_chain_branch()
    assert "for published_url in ${PUBLISHED_INDEX:-}" in branch
    assert "--repofrompath" in branch and "tunaos${published_n}" in branch


def test_the_local_artifacts_still_outrank_the_published_repo():
    """Priority 1 local vs 50 published. If the published repo could win, a
    cell would verify against a previously published build of itself rather
    than the artifacts it just produced."""
    branch = build_chain_branch()
    assert "--setopt=factory.priority=1" in branch
    assert "priority=50" in branch


def test_the_empty_array_expansion_is_guarded():
    """`"${a[@]}"` on an empty array is an unbound-variable error under set -u
    on bash < 4.4, and the probe images are not all bash 5. A cell whose
    target declares no published index must not die on the expansion."""
    assert '${published_args[@]+"${published_args[@]}"}' in build_chain_branch()


def test_the_pulseaudio_plugin_requires_a_capability_that_exists_on_el10():
    """pipewire-pulseaudio provides pulseaudio-daemon and obsoletes
    pulseaudio, so the bare name can never resolve."""
    spec = SPEC.read_text(encoding="utf-8")
    assert "Requires: pulseaudio-daemon" in spec
    assert "\nRequires: pulseaudio\n" not in spec


def test_it_matches_what_upstream_fedora_requires():
    """Fedora's own xfce4-pulseaudio-plugin.spec requires pulseaudio-daemon
    and pavucontrol. Diverging would be a decision needing its own reason."""
    spec = SPEC.read_text(encoding="utf-8")
    assert "Requires: pavucontrol" in spec
