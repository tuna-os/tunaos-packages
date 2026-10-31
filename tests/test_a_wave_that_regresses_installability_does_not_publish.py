"""A Hummingbird wave may fix blockers; it may not add one (#629).

Two things this pins, both measured rather than assumed:

* **Not every unresolved capability is a package to build.**  On 2026-10-24
  the served prefix still answered `libm.so.6(GLIBC_2.44)(64bit)` for
  `libavutil-free`, `libavfilter-free` and `zvbi` -- the Rawhide-buildroot
  leak docs/HUMMINGBIRD-TARGET.md §2 named in September.  The target DOES
  provide `libm.so.6()(64bit)`; what it does not provide is that symbol
  version, so no amount of further building closes it and the build-order
  arithmetic never notices.  `classify()` separates that class (rebuild in
  the target's root) from the capability nothing provides at any version
  (build it), and `rebuild_list()` is the list docs/HUMMINGBIRD-TARGET.md §6
  asked a human to read out of a column.

* **The gate has to run before the bucket hears the answer.**  Publishing is
  `rclone sync` into R2 with no undo, so a wave that makes a desktop
  unresolvable is what consumers get until the next wave.  The check runs on
  the freshly indexed local tree -- the exact bytes about to be synced -- and
  refuses the sync when a desktop lost ground against
  docs/hummingbird-installability-baseline.json.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chk = _load("check_installability", "check-hummingbird-installability.py")
gap = _load("gap_engine", "gap_engine.py")

WORKFLOW = ROOT / ".github" / "workflows" / "publish-build-chain-rpms.yml"
BASELINE = ROOT / "docs" / "hummingbird-installability-baseline.json"


def index(packages: dict[str, dict]) -> dict:
    """A parsed-primary-shaped index from {name: {requires, provides}}."""
    out = {"packages": {}, "provides": {}, "provides_evr": {}, "files": set()}
    for name, spec in packages.items():
        out["packages"][name] = {
            "arch": "x86_64", "evr": "1-1", "srpm": f"{name}-1-1.src.rpm",
            "requires": list(spec.get("requires", [])),
            "requires_versioned": [],
        }
        out["provides"].setdefault(name, set()).add(name)
        for cap in spec.get("provides", []):
            out["provides"].setdefault(cap, set()).add(name)
    return out


CATALOG = {"desktops": {
    "tiny": {"required_packages": ["desktop-shell"],
             "install_packages": ["desktop-shell"]},
}}


def classify(cap: str, provided: list[str]) -> str:
    provides = {name: {"x"} for name in provided}
    return chk.classify(cap, provides, chk.soname_families(provides))


def test_a_symbol_version_the_target_cannot_provide_is_a_rebuild_not_a_build():
    # The live shape: glibc 2.43 provides the library, our package wants 2.44.
    assert classify("libm.so.6(GLIBC_2.44)(64bit)",
                    ["libm.so.6()(64bit)"]) == "abi_symbol"
    # Same shape, other symbol namespaces the leak audit named.
    assert classify("libcrypto.so.3(OPENSSL_4.0)(64bit)",
                    ["libcrypto.so.3()(64bit)"]) == "abi_symbol"


def test_a_soname_nothing_serves_at_that_major_is_a_rebuild_against_what_is_served():
    # openxr-libs/waybar want libjsoncpp.so.26; the base serves .so.25.
    assert classify("libjsoncpp.so.26()(64bit)",
                    ["libjsoncpp.so.25()(64bit)"]) == "abi_soname"


def test_a_capability_no_repository_serves_at_any_version_is_a_package_to_build():
    # libcanberra: nothing anywhere provides the name, so this IS build work.
    assert classify("libcanberra.so.0()(64bit)", ["libm.so.6()(64bit)"]) == "absent"
    # A symbol version whose library is also absent stays build work: samba.
    assert classify("libsmbclient.so.0(SMBCLIENT_0.1.0)(64bit)", []) == "absent"
    # Package-name capabilities are never an ABI question.
    assert classify("poppler-data", []) == "absent"
    assert classify("gstreamer1-plugins-bad-free-libs(x86-64)", []) == "absent"


def test_only_our_own_published_output_lands_on_the_rebuild_list():
    target = index({"glibc": {"provides": ["libm.so.6()(64bit)"]}})
    published = index({
        "desktop-shell": {"requires": ["libavutil-free", "libcanberra.so.0()(64bit)"]},
        "libavutil-free": {"requires": ["libm.so.6(GLIBC_2.44)(64bit)"]},
    })
    report = chk.check(CATALOG, target, published, ["tiny"])
    detail = report["tiny"]["unresolved"]
    assert detail["libm.so.6(GLIBC_2.44)(64bit)"]["class"] == "abi_symbol"
    assert detail["libcanberra.so.0()(64bit)"]["class"] == "absent"
    assert report["tiny"]["leaks"] == ["libm.so.6(GLIBC_2.44)(64bit)"]
    # The thing to do about it, named per package, not per capability.
    assert chk.rebuild_list(report) == {
        "libavutil-free": ["libm.so.6(GLIBC_2.44)(64bit)"]}


def test_an_abi_break_the_target_itself_carries_is_not_ours_to_rebuild():
    """`needer_from: target` is the base OS's own skew -- reporting it is
    right, putting it on this factory's rebuild list is not."""
    target = index({
        "glibc": {"provides": ["libm.so.6()(64bit)"]},
        "base-tool": {"requires": ["libm.so.6(GLIBC_2.44)(64bit)"]},
    })
    published = index({"desktop-shell": {"requires": ["base-tool"]}})
    report = chk.check(CATALOG, target, published, ["tiny"])
    assert report["tiny"]["unresolved"]["libm.so.6(GLIBC_2.44)(64bit)"]["class"] == "abi_symbol"
    assert report["tiny"]["leaks"] == []
    assert chk.rebuild_list(report) == {}


def _report(unresolved: list[str], *, absent: list[str] = (), resolvable=False):
    return {"tiny": {
        "roots": 1, "roots_absent": list(absent), "closure_packages": 1,
        "unresolved": {cap: {"needed_by": ["desktop-shell"],
                             "needer_from": ["published"], "class": "absent"}
                       for cap in unresolved},
        "leaks": [], "resolvable": resolvable,
    }}


def test_a_wave_that_adds_a_blocker_regresses_even_when_it_fixes_another():
    """Counts are not the contract: one fixed, one broken keeps the count."""
    baseline = chk.baseline_of(_report(["libcanberra.so.0()(64bit)"]))
    verdict = chk.compare(baseline, _report(["libvorbis.so.0()(64bit)"]))
    assert chk.regressed(verdict)
    assert verdict["tiny"]["regressions"] == ["libvorbis.so.0()(64bit)"]
    assert verdict["tiny"]["fixed"] == ["libcanberra.so.0()(64bit)"]


def test_a_wave_that_only_fixes_blockers_publishes():
    baseline = chk.baseline_of(_report(["libcanberra.so.0()(64bit)",
                                        "libvorbis.so.0()(64bit)"]))
    verdict = chk.compare(baseline, _report(["libvorbis.so.0()(64bit)"]))
    assert not chk.regressed(verdict)
    assert verdict["tiny"]["fixed"] == ["libcanberra.so.0()(64bit)"]


def test_a_root_that_stops_being_served_is_a_regression_too():
    """A wave can also break a desktop by dropping a root name from the
    prefix -- the #519 shape, where the sync lost names rather than caps."""
    baseline = chk.baseline_of(_report([]))
    verdict = chk.compare(baseline, _report([], absent=["gnome-shell"]))
    assert chk.regressed(verdict)
    assert verdict["tiny"]["regressions"] == ["gnome-shell"]


def test_a_desktop_that_was_green_and_is_not_fails_even_with_a_smaller_set():
    baseline = chk.baseline_of(_report([], resolvable=True))
    verdict = chk.compare(baseline, _report([], absent=["gnome-shell"]))
    assert verdict["tiny"]["lost_resolvable"]
    assert chk.regressed(verdict)


def test_a_desktop_the_baseline_never_measured_is_recorded_not_gated():
    verdict = chk.compare({}, _report(["libcanberra.so.0()(64bit)"]))
    assert verdict["tiny"]["new"]
    assert not chk.regressed(verdict)


def test_writing_one_architectures_baseline_leaves_the_other_alone(tmp_path):
    path = tmp_path / "baseline.json"
    measurement = {"measured_at": "2026-10-24T00:00:00+00:00",
                   "target_index": {"baseurl": "t"}, "published_index": {"baseurl": "p"},
                   "consumed_indexes": []}
    chk.write_baseline(path, "x86_64", measurement, _report(["a"]))
    chk.write_baseline(path, "aarch64", measurement, _report(["b"]))
    recorded = json.loads(path.read_text())
    assert sorted(recorded["arches"]) == ["aarch64", "x86_64"]
    assert recorded["arches"]["x86_64"]["desktops"]["tiny"]["unresolved"] == ["a"]
    assert recorded["arches"]["aarch64"]["desktops"]["tiny"]["unresolved"] == ["b"]
    # Provenance travels with the numbers: a baseline nobody can re-derive is
    # a number, not a measurement.
    assert recorded["arches"]["x86_64"]["published_index"] == {"baseurl": "p"}


def test_the_committed_baseline_covers_every_architecture_the_target_publishes():
    factory = yaml.safe_load((ROOT / "manifests" / "package-factory.yaml").read_text())
    arches = set(factory["targets"]["hummingbird"]["published_index"])
    recorded = json.loads(BASELINE.read_text())
    assert set(recorded["arches"]) == arches, (
        "the publish gate refuses an architecture it has no baseline for; a "
        "published arch without one can never publish")
    catalog = yaml.safe_load((ROOT / "manifests" / "hummingbird-desktops.yaml").read_text())
    desktops = {d for d in catalog["desktops"] if d != "bluefin"}
    for arch, recording in recorded["arches"].items():
        assert set(recording["desktops"]) == desktops, arch
        assert recording["published_index"]["primary_sha256"], arch


def _repodata(tmp_path: pathlib.Path, primary: bytes) -> pathlib.Path:
    """The smallest tree gap_engine.primary_of() will read off local disk."""
    repo = tmp_path / "repo"
    (repo / "repodata").mkdir(parents=True)
    (repo / "repodata" / "primary.xml.gz").write_bytes(gzip.compress(primary))
    (repo / "repodata" / "repomd.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<repomd xmlns="http://linux.duke.edu/metadata/repo">'
        "<revision>1</revision>"
        '<data type="primary">'
        '<checksum type="sha256">unverified</checksum>'
        '<location href="repodata/primary.xml.gz"/>'
        "</data></repomd>\n")
    return repo


PRIMARY = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
           b'<metadata xmlns="http://linux.duke.edu/metadata/common" '
           b'xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="1">'
           b'<package type="rpm"><name>%s</name><arch>x86_64</arch>'
           b'<version epoch="0" ver="1" rel="1"/>'
           b"<format></format></package></metadata>\n")


def test_the_gate_reads_the_tree_on_disk_and_never_a_cached_copy_of_it(tmp_path):
    """publish-rpm-wave.sh indexes `repo/` and the gate reads it in the same
    run: a cache keyed by URL would answer with the pre-wave index."""
    repo = _repodata(tmp_path, PRIMARY % b"before")
    url = f"file://{repo}"
    cache = tmp_path / "cache"
    first = gap.parse_primary(gap.primary_of(url, cache)[0])
    assert set(first["packages"]) == {"before"}
    (repo / "repodata" / "primary.xml.gz").write_bytes(
        gzip.compress(PRIMARY % b"after"))
    second = gap.parse_primary(gap.primary_of(url, cache)[0])
    assert set(second["packages"]) == {"after"}
    assert not cache.exists(), "a local tree must not be cached at all"


def _publish_job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["publish"]


def test_the_publish_job_asks_before_it_syncs_and_only_for_hummingbird():
    steps = _publish_job()["steps"]
    names = [step.get("name", "") for step in steps]
    gate = names.index("Refuse a wave that regresses installability")
    assert names.index("Sign and index") < gate < names.index("Sync up"), (
        "the tree must already be indexed, and the answer must still be able "
        "to stop the sync")
    step = steps[gate]
    assert step["if"] == "${{ matrix.target == 'hummingbird' }}", (
        "every other family has its own target repository and no baseline here")
    run = step["run"]
    assert "check-hummingbird-installability.py" in run
    assert "--fail-on-regression" in run, "a report that cannot fail is not a gate"
    assert 'file://${PWD}/repo' in run, (
        "the wave's own freshly indexed tree, not the index R2 still serves")
    assert "--arch '${{ matrix.architecture }}'" in run


lister = _load("list_served_nvrs", "list-served-nvrs.py")

SERVED = """<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://linux.duke.edu/metadata/common"
          xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="4">
<package type="rpm"><name>ffmpeg-free</name>
<version epoch="0" ver="8.1.2" rel="12.1.bfin1"/>
<format><rpm:sourcerpm>ffmpeg-8.1.2-12.1.bfin1.src.rpm</rpm:sourcerpm></format>
</package>
<package type="rpm"><name>libavutil-free</name>
<version epoch="0" ver="8.1.2" rel="12.1.bfin1"/>
<format><rpm:sourcerpm>ffmpeg-8.1.2-12.1.bfin1.src.rpm</rpm:sourcerpm></format>
</package>
<package type="rpm"><name>zvbi</name>
<version epoch="0" ver="0.2.45" rel="1.1.bfin1"/>
<format><rpm:sourcerpm>zvbi-0.2.45-1.1.bfin1.src.rpm</rpm:sourcerpm></format>
</package>
<package type="rpm"><name>gtk4</name>
<version epoch="0" ver="4.22.1" rel="1.bfin1"/>
<format><rpm:sourcerpm>gtk4-4.22.1-1.bfin1.src.rpm</rpm:sourcerpm></format>
</package>
</metadata>
"""


def test_withholding_a_leaked_binary_withholds_its_whole_source_build():
    """`rpmspec -q` keys the chain's skip on the spec's FIRST binary, so
    withholding `libavutil-free` alone leaves `ffmpeg-free` served, ffmpeg
    skipped, and the leak exactly where it was."""
    assert lister.nvrs(SERVED, {"libavutil-free"}) == [
        "zvbi-0.2.45-1.1.bfin1", "gtk4-4.22.1-1.bfin1"]


def test_everything_else_the_index_serves_still_counts_as_built():
    """The skip is what keeps a leg incremental (#544); withholding more
    than the leak would send the chain back to tier 0."""
    assert lister.nvrs(SERVED, set()) == [
        "ffmpeg-free-8.1.2-12.1.bfin1", "libavutil-free-8.1.2-12.1.bfin1",
        "zvbi-0.2.45-1.1.bfin1", "gtk4-4.22.1-1.bfin1"]
    assert lister.nvrs(SERVED, {"zvbi"}) == [
        "ffmpeg-free-8.1.2-12.1.bfin1", "libavutil-free-8.1.2-12.1.bfin1",
        "gtk4-4.22.1-1.bfin1"]


def test_a_name_no_longer_leaking_comes_back_into_the_skip_list():
    """The list is measured per run, not committed: once the rebuild lands,
    nothing withholds it and the chain stops rebuilding it every leg."""
    assert "gtk4-4.22.1-1.bfin1" in lister.nvrs(SERVED, {"libavutil-free"})


def test_both_chain_callers_withhold_what_the_checker_names():
    for path, target_expr in (
        (WORKFLOW, '"$TARGET" == hummingbird'),
        (ROOT / ".github" / "workflows" / "chain-band.yml",
         "'${{ inputs.target }}' = hummingbird"),
    ):
        text = path.read_text(encoding="utf-8")
        step = [s for s in yaml.safe_load(text)["jobs"].values()
                for s in s["steps"]
                if "list-served-nvrs.py" in str(s.get("run", ""))]
        assert step, path.name
        run = step[0]["run"]
        assert target_expr in run, (
            f"{path.name}: only hummingbird has an installability baseline")
        assert "--print-rebuild-names" in run and "--except-names" in run, (
            f"{path.name}: a served package the target cannot install must "
            "not count as built, or the leak outlives every wave")
