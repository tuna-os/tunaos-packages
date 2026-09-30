"""The hummingbird buildroot must be able to see what the factory published.

dconf failed every build for days, and with it every publish: the cell exits
non-zero on any failed package, so `Record a new ActionResult` is skipped and
588 successfully built packages never reach the repo.

The cause was not dconf. dconf BuildRequires vala; vala BuildRequires
gobject-introspection-devel; and Rawhide's carries the rich dependency
`(python(abi) = 3.15 if python3)`. python3 IS in the chroot -- Hummingbird's
3.14 -- so it fires:

    cannot install both python3-3.15.0~rc1-1.fc45.x86_64 from fedora
    and python3-3.14.3-2.1.hum1.x86_64 from hummingbird

The factory had already built both against the right interpreter and published
them. The buildroot just had no repo pointing at its own output, so every
resolution fell through to Rawhide.

These tests pin the three things that make the fix real, and each is a
separate failure this repo has already paid for once:

  * BOTH arch configs carry it. Fixing one file and leaving the identical
    line in its twin is the #529 epoch shape.
  * The URL agrees with the target's declared published_index, so the
    buildroot reads the prefix the publisher writes.
  * Priority sits below the base OS, so this fills gaps rather than shadowing
    Hummingbird's own packages with our rebuilds.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = [
    ROOT / "mock/hummingbird-ci.cfg",
    ROOT / "mock/hummingbird-ci-aarch64.cfg",
]
REPO_ID = "tunaos-hummingbird"


def repo_block(text: str, repo_id: str) -> str:
    match = re.search(rf"^\[{re.escape(repo_id)}\]$(.*?)(?=^\[|\Z)",
                      text, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def setting(block: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}=(.*)$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name)
def test_both_arch_buildroots_read_the_factory_output(config):
    block = repo_block(config.read_text(encoding="utf-8"), REPO_ID)
    assert block, (
        f"{config.name} has no [{REPO_ID}] repo. Without it the buildroot "
        "cannot see gobject-introspection-devel or vala built against "
        "python 3.14, and falls through to Rawhide's 3.15 build, which "
        "cannot be installed beside Hummingbird's interpreter."
    )


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name)
def test_it_reads_the_prefix_the_publisher_writes(config):
    """A buildroot pointed at a prefix nobody publishes to reads an empty repo."""
    factory = yaml.safe_load((ROOT / "manifests/package-factory.yaml").read_text())
    declared = factory["targets"]["hummingbird"]["published_index"]
    urls = []
    for value in declared.values():
        urls.extend(value if isinstance(value, list) else [value])

    baseurl = setting(repo_block(config.read_text(encoding="utf-8"), REPO_ID), "baseurl")
    assert baseurl, f"{config.name}: [{REPO_ID}] has no baseurl"
    # mock substitutes $basearch; compare on the arch-independent stem.
    stem = baseurl.replace("$basearch", "")
    assert any(url.replace("x86_64", "").replace("aarch64", "") == stem for url in urls), (
        f"{config.name}: [{REPO_ID}] baseurl {baseurl!r} does not match any "
        f"published_index for hummingbird ({urls}). The buildroot would read "
        "a prefix the publisher never writes to."
    )


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name)
def test_the_base_os_still_outranks_our_rebuilds(config):
    """Gap-filling, not shadowing.

    Lower priority wins in dnf. Our output must rank BELOW [hummingbird] so
    the base OS keeps ownership of everything it ships, and ABOVE the Fedora
    repos so it beats Rawhide -- which is the whole point.
    """
    text = config.read_text(encoding="utf-8")
    ours = int(setting(repo_block(text, REPO_ID), "priority"))
    base = int(setting(repo_block(text, "hummingbird"), "priority"))
    local = int(setting(repo_block(text, "local-build"), "priority"))
    # Fedora 44's [fedora]/[updates] come from the included mock template and
    # are re-prioritised in place by the config (see
    # test_hummingbird_buildroot_is_fedora44_plus_hummingbird.py); read the
    # priority the rewrite assigns.
    match = re.search(r'\\npriority=(\d+)\\n', text)
    assert match, f"{config.name}: no in-place Fedora priority rewrite found"
    fedora = int(match.group(1))
    assert local < base < ours < fedora, (
        f"{config.name}: priorities must order local-build({local}) < "
        f"hummingbird({base}) < {REPO_ID}({ours}) < fedora({fedora}). "
        "Above the base OS would shadow Hummingbird's own packages with our "
        "rebuilds; below Fedora would leave the build root's own packages "
        "winning over the desktop stack we exist to supply."
    )
