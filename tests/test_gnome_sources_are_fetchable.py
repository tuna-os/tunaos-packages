"""Every source a spec declares must be something the build can actually get.

Both gnome cells failed on packages whose sources could not be fetched, and
both failures were arch-independent — the aarch64 cells merely ran first
(#480).

  malcontent   Source2 `gvdb.tar.xz` and Source3 `tinycdb-0.81.tar.gz` were
               BARE FILENAMES with no URL, and neither file existed anywhere
               in the repository. spectool fetches what has a URL; the SRPM
               build then died on `Bad file: … No such file or directory`.

  libnotify    not a fetch problem but the same shape: gnome-settings-daemon
               requires >= 0.8.7 and EL10 tops out at 0.8.6, so the build
               depended on something no reachable repository provided.

The rule this file encodes is narrow on purpose: a numbered Source in a spec
under src/ must either carry a URL or exist as a file beside the spec. A
bare filename that is neither is unbuildable by construction, and that is
exactly what shipped.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = re.compile(r"^Source(\d*):\s*(\S+)", re.MULTILINE)
HAS_SCHEME = re.compile(r"^(https?|ftp)://")


def declared_sources(spec: Path) -> list[tuple[str, str]]:
    return SOURCE.findall(spec.read_text(encoding="utf-8", errors="replace"))


def unfetchable(spec: Path) -> list[str]:
    """Sources that nothing in the build can obtain.

    Three legitimate ways a spec gets a source, and a bare filename is only a
    defect when it is none of them:

      a URL          spectool downloads it.
      a file beside  the spec directory carries it in-tree.
      the lookaside  the directory has a Fedora distgit `sources` manifest
                     (`SHA512 (file) = …`), and the fetch goes through that.

    The third is why src/gnome-50/mutter, src/hummingbird/plasma-desktop and
    src/deps/selinux-policy carry bare filenames and build perfectly well.
    src/deps/malcontent had no `sources` manifest and no file — only the spec —
    which is exactly why its bare `gvdb.tar.xz` and `tinycdb-0.81.tar.gz` could
    never be fetched (#480).
    """
    if (spec.parent / "sources").is_file():
        return []
    missing = []
    for _, value in declared_sources(spec):
        if HAS_SCHEME.match(value) or "%{" in value:
            continue
        if not (spec.parent / value).is_file():
            missing.append(value)
    return missing


def built_spec_dirs() -> set[Path]:
    """Only the specs some build order actually walks.

    Scoped deliberately. src/ also holds specs nothing builds — a
    distgit-imported mutter-rawhide.spec whose sources come from Fedora's
    lookaside cache — and for those a bare filename is
    not a defect because no tier ever fetches them. A rule that flagged them
    would be noise, and noise gets suppressed rather than fixed.
    """
    import yaml

    dirs: set[Path] = set()
    for manifest in sorted(ROOT.glob("build-order*.yml")):
        spec = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        for tier in spec.get("tiers") or []:
            for package in tier.get("packages") or []:
                path = package.get("path")
                if path:
                    dirs.add(ROOT / path)
    return dirs


def test_there_are_specs_to_check():
    assert len(built_spec_dirs()) > 40


def test_no_built_spec_declares_a_source_nothing_can_fetch():
    offenders = {}
    for directory in sorted(built_spec_dirs()):
        for spec in sorted(directory.glob("*.spec")):
            if missing := unfetchable(spec):
                offenders[str(spec.relative_to(ROOT))] = missing
    assert not offenders, offenders


def test_the_rule_catches_the_malcontent_shape(tmp_path):
    """Mutation in miniature, so the rule is proven against the original
    defect rather than merely passing today."""
    spec = tmp_path / "broken.spec"
    spec.write_text(
        "Name: x\n"
        "Source0: https://example.invalid/x-1.tar.xz\n"
        "Source2: gvdb.tar.xz\n",
        encoding="utf-8",
    )
    assert unfetchable(spec) == ["gvdb.tar.xz"]

    # ...and the lookaside exemption is not a blanket one: it applies only
    # where a `sources` manifest actually exists.
    (tmp_path / "sources").write_text("SHA512 (gvdb.tar.xz) = abc\n", encoding="utf-8")
    assert unfetchable(spec) == []


def test_malcontent_fetches_tinycdb_from_its_own_wrap_url():
    """malcontent's subprojects/tinycdb.wrap declares this source_url and a
    source_hash that the served tarball matches, so this is upstream's own
    answer rather than one invented here."""
    text = (ROOT / "src" / "deps" / "malcontent" / "malcontent.spec").read_text(encoding="utf-8")
    assert "https://www.corpit.ru/mjt/tinycdb/tinycdb-0.81.tar.gz" in text


def test_malcontent_no_longer_carries_a_redundant_gvdb_source():
    """malcontent's release tarball already ships subprojects/gvdb populated,
    so the extra source and its `tar -xf` were doing nothing but breaking the
    build."""
    text = (ROOT / "src" / "deps" / "malcontent" / "malcontent.spec").read_text(encoding="utf-8")
    assert "gvdb.tar.xz" not in text
    assert "%{SOURCE3}" not in text, "a source was dropped but its extraction was left behind"


def test_libnotify_is_built_before_the_package_that_needs_it():
    """gnome-settings-daemon requires >= 0.8.7; the koji buildroot has 0.8.6 on
    both arches and EPEL has none. Building it is the only way to satisfy that,
    and it has to land in an earlier tier."""
    import yaml

    for manifest in ("build-order.yml", "build-order-gnome51.yml"):
        tiers = yaml.safe_load((ROOT / manifest).read_text(encoding="utf-8"))["tiers"]
        def tier_of(name: str) -> int:
            for i, tier in enumerate(tiers):
                for pkg in tier.get("packages", []):
                    if pkg.get("path", "").rsplit("/", 1)[-1] == name:
                        return i
            raise AssertionError(f"{name} not in {manifest}")
        assert tier_of("libnotify") < tier_of("gnome-settings-daemon"), manifest


def test_libnotify_does_not_build_its_tests():
    """Load-bearing: libnotify's gtk4 dependency is conditional on the tests
    option, and gtk4 is built by this same chain in the SAME tier. Enabling
    tests would make a tier depend on itself."""
    text = (ROOT / "src" / "deps" / "libnotify" / "libnotify.spec").read_text(encoding="utf-8")
    assert "-Dtests=false" in text
