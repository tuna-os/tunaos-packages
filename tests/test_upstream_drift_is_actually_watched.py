"""Hummingbird ships fixes as soon as upstream publishes them. We did not.

The point of this target is speed: near-zero CVE, per-package lifecycle,
rolling with Rawhide. A factory measuring its gap against a weeks-old snapshot
cannot serve that — it rebuilds what upstream has already adopted and misses
what upstream has just changed.

manifests/package-factory.yaml has always declared what to do:

    gap_measurement:
      target_index: https://.../public-hummingbird/$arch/
      drift: {mode: propose, report_json: docs/hummingbird-desktop-gap.json}

and nothing implemented it. .github/workflows/hummingbird-gap-drift.yml did,
until 6d4b77a removed it along with everything else hummingbird-specific
("de-hardcode the pipeline from hummingbird", #517). measure-target-gap.py was
correctly generalised and kept. The reactive driver was not replaced, so that
declaration sat inert.

Measured 2026-08-25 against the live index:

    live upstream revision   1787625027   2026-08-25T02:30:27Z
    committed measurement    1787045128   2026-08-18T09:25:28Z

Upstream had published about five hours earlier. Nothing in the repository
knew, and nothing would have said so.
"""

import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cud", ROOT / "scripts" / "check-upstream-drift.py")
cud = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cud)

REPOMD = ('<?xml version="1.0"?><repomd xmlns="http://linux.duke.edu/metadata/repo">'
          "<revision>{rev}</revision></repomd>")


def _manifest(**drift):
    d = {"mode": "propose", "build_order": "bo.yml", "report_json": "r.json"}
    d.update(drift)
    return {"targets": {"t": {"gap_measurement": {
        "target_index": "https://example.invalid/x/$arch/", "drift": d}}}}


def test_a_moved_upstream_is_reported_as_drift(tmp_path):
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"target_index": {"revision": "111"}}))
    out = cud.evaluate(_manifest(report_json=str(report)),
                       opener=lambda url: REPOMD.format(rev="222").encode())
    assert [r["state"] for r in out] == ["drifted"]
    assert out[0]["live"] == "222" and out[0]["recorded"] == "111"


def test_an_unmoved_upstream_is_current(tmp_path):
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"target_index": {"revision": "111"}}))
    out = cud.evaluate(_manifest(report_json=str(report)),
                       opener=lambda url: REPOMD.format(rev="111").encode())
    assert [r["state"] for r in out] == ["current"]


def test_an_unreadable_index_is_never_reported_as_current(tmp_path):
    """The failure this file exists to prevent.

    A check that cannot reach upstream must not answer 'no drift'. That is
    indistinguishable from a healthy result and lets a stale measurement live
    forever behind a green check.
    """
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"target_index": {"revision": "111"}}))

    def boom(url):
        raise OSError("network down")

    out = cud.evaluate(_manifest(report_json=str(report)), opener=boom)
    assert [r["state"] for r in out] == ["unknown"]
    assert out[0]["state"] != "current"


def test_malformed_repomd_is_unknown_not_current(tmp_path):
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"target_index": {"revision": "111"}}))
    out = cud.evaluate(_manifest(report_json=str(report)),
                       opener=lambda url: b"<not xml")
    assert [r["state"] for r in out] == ["unknown"]


def test_never_measured_is_distinct_from_unreadable(tmp_path):
    """fedora is in this state today; conflating the two hides real failures."""
    out = cud.evaluate(_manifest(report_json=str(tmp_path / "absent.json")),
                       opener=lambda url: REPOMD.format(rev="222").encode())
    assert [r["state"] for r in out] == ["unmeasured"]


def test_the_arch_placeholder_is_substituted():
    seen = {}

    def spy(url):
        seen["url"] = url
        return REPOMD.format(rev="1").encode()

    cud.evaluate(_manifest(), opener=spy)
    assert "$arch" not in seen["url"], "the $arch placeholder reached the network"
    assert cud.PROBE_ARCH in seen["url"]


def test_a_target_without_a_drift_block_is_not_watched():
    m = {"targets": {"a": {"gap_measurement": {"target_index": "u"}},
                     "b": {"status": "supported"}}}
    assert cud.drift_targets(m) == []


# --- guard the guard -------------------------------------------------------

def test_the_real_manifest_still_declares_something_to_watch():
    """If the manifest shape changes, every test above passes while the
    checker examines nothing. Assert against the committed manifest."""
    with open(ROOT / "manifests" / "package-factory.yaml", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    watched = cud.drift_targets(manifest)
    assert watched, (
        "no target declares gap_measurement.drift — either the manifest shape "
        "moved or this checker has gone blind"
    )
    assert any(name == "hummingbird" for name, _ in watched), (
        "hummingbird is the target whose entire purpose is reacting to "
        "upstream; it must be watched"
    )


def test_the_watched_targets_declare_a_reachable_shaped_index():
    with open(ROOT / "manifests" / "package-factory.yaml", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    for name, gap in cud.drift_targets(manifest):
        idx = str(gap.get("target_index", ""))
        assert idx.startswith("http"), f"{name}: target_index is not a URL: {idx!r}"
        assert gap["drift"].get("report_json"), f"{name}: drift has no report_json"


def test_force_all_selects_every_declared_target(tmp_path, monkeypatch):
    """A `force` that yields an empty matrix is a button that does nothing.

    The remeasure job is skipped when its matrix has no cells, so the run goes
    green having measured no target — the exact outcome this workflow exists
    to prevent. Forcing must therefore select every declared target, not the
    drifted subset.
    """
    import subprocess
    import sys

    gho = tmp_path / "out"
    env = {**dict(__import__("os").environ), "GITHUB_OUTPUT": str(gho)}
    script = str(ROOT / "scripts" / "check-upstream-drift.py")

    # Point at a manifest whose single target cannot possibly have drifted:
    # an unreachable index means state != "drifted", so an unforced run must
    # emit an empty matrix and a forced one must not.
    man = tmp_path / "m.yaml"
    man.write_text(yaml.safe_dump({"targets": {"t": {"gap_measurement": {
        "target_index": "https://127.0.0.1:1/x/$arch/",
        "drift": {"mode": "propose", "report_json": str(tmp_path / "absent.json")},
    }}}}))

    subprocess.run([sys.executable, script, "--manifest", str(man),
                    "--github-output"], env=env, capture_output=True, timeout=120)
    assert 'matrix=[]' in gho.read_text(), "unforced run should select nothing here"

    gho.write_text("")
    subprocess.run([sys.executable, script, "--manifest", str(man),
                    "--github-output", "--force-all"], env=env,
                   capture_output=True, timeout=120)
    text = gho.read_text()
    assert 'matrix=["t"]' in text, f"force-all selected nothing: {text!r}"
    assert "any=true" in text, "force-all must also flip the gate that runs the job"


def test_a_mode_nothing_implements_is_reported_not_silently_proposed(tmp_path):
    """`mode` was read into the output and acted on nowhere.

    upstream-drift.yml always PROPOSES -- it opens a pull request with the
    regenerated measurement -- so a target declaring `mode: apply` would read
    as "commit straight to main" and silently get a pull request instead. The
    manifest would describe behaviour the factory does not have.

    Found live: the real manifest declares `mode: exhibit` for fedora, which
    nothing implements.
    """
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"target_index": {"revision": "111"}}))
    out = cud.evaluate(_manifest(report_json=str(report), mode="apply"),
                       opener=lambda url: REPOMD.format(rev="222").encode())
    assert [r["state"] for r in out] == ["unimplemented-mode"], (
        "a mode the driver does not implement was accepted; the target would "
        "be re-measured and the manifest would keep describing behaviour that "
        "does not exist"
    )


def test_one_misdeclared_target_does_not_blind_the_others(tmp_path):
    """The whole point of a reactive detector is that it keeps reacting.

    Aborting the run because one target is misdeclared would stop every other
    target from being measured -- the same shape as the unreadable-index case
    this script already handles by degrading that target alone.
    """
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"target_index": {"revision": "111"}}))
    manifest = {"targets": {
        "bad": {"gap_measurement": {
            "target_index": "https://example.invalid/x/$arch/",
            "drift": {"mode": "exhibit", "build_order": "bo.yml",
                      "report_json": str(report)}}},
        "good": {"gap_measurement": {
            "target_index": "https://example.invalid/y/$arch/",
            "drift": {"mode": "propose", "build_order": "bo.yml",
                      "report_json": str(report)}}},
    }}
    out = cud.evaluate(manifest,
                       opener=lambda url: REPOMD.format(rev="222").encode())
    states = {r["target"]: r["state"] for r in out}
    assert states == {"bad": "unimplemented-mode", "good": "drifted"}, (
        "a misdeclared target changed the verdict for a correctly declared "
        f"one: {states}"
    )


def test_the_implemented_mode_set_matches_what_the_workflow_does() -> None:
    """A guard on the guard.

    If someone implements `apply` in the workflow, this set must grow with
    it; if the set grows without the workflow changing, the check goes back
    to rubber-stamping. Pinning `propose` against the workflow's actual
    create-pull-request step keeps the two honest.
    """
    assert cud.IMPLEMENTED_MODES == {"propose"}
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "upstream-drift.yml").read_text())
    uses = [str(s.get("uses", ""))
            for s in workflow["jobs"]["remeasure"]["steps"]]
    assert any("create-pull-request" in u for u in uses), (
        "upstream-drift.yml no longer opens a pull request, so `propose` is "
        "no longer what it implements"
    )
