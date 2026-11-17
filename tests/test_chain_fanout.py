"""The fan-out must build everything exactly once, and publish like one.

Three layers, three failure modes:

  * the PLANNER must cover the whole order with disjoint shards -- a package
    dealt twice builds twice (wasted runner), a package dealt never silently
    falls out of the repo;
  * the CHAIN must treat filters and served-NVRs as opt-in -- the
    single-runner publisher passes neither and must behave exactly as before;
  * the WORKFLOWS must keep the one-publisher invariant -- the fanout's
    publish job rides the same `publish-rpms` concurrency group as every
    other publisher, and only ONE collector writes each band's cumulative
    artifact.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
ORDER = ROOT / "build-order-hummingbird-desktops.yml"
FANOUT = ROOT / ".github" / "workflows" / "build-chain-fanout.yml"
BAND = ROOT / ".github" / "workflows" / "chain-band.yml"
CHAIN = ROOT / "scripts" / "build-chain.sh"
SOURCES = ROOT / "scripts" / "lib" / "build-chain" / "sources.sh"


@pytest.fixture(scope="module")
def plan():
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "plan-chain-shards.py"),
         "--order", str(ORDER), "--bands", "5", "--shards", "8"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    ).stdout
    return json.loads(out)


@pytest.fixture(scope="module")
def order_packages():
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "parse-build-order.py"),
         str(ORDER), "--all"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [line.split("\t")[0] for line in out.splitlines() if line.strip()]


def test_every_package_dealt_exactly_once(plan, order_packages):
    dealt = [p for b in plan["bands"] for s in b["shards"] for p in s]
    assert sorted(dealt) == sorted(order_packages), (
        "a package dealt twice builds twice; one dealt never silently "
        "falls out of the repo"
    )


def test_bands_are_contiguous_tier_runs(plan):
    tiers = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "parse-build-order.py"),
         str(ORDER), "--tiers"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    flattened = [t for b in plan["bands"] for t in b["tiers"].split(",")]
    assert flattened == tiers, "band boundaries must respect tier order"


def test_shards_are_balanced(plan):
    for b in plan["bands"]:
        sizes = [len(s) for s in b["shards"]]
        assert max(sizes) - min(sizes) <= 1, (
            f"band {b['index']}: one hot shard sets the band's wall-clock"
        )


def test_no_empty_shards(plan):
    for b in plan["bands"]:
        assert all(b["shards"]), "an empty shard schedules a runner for nothing"


# --- build-chain flags stay opt-in ------------------------------------------

def test_chain_filters_are_opt_in():
    text = CHAIN.read_text(encoding="utf-8")
    assert '--packages-file) FILTER_PACKAGES_FILE="$2"' in text
    assert '--served-nvrs)   SERVED_NVRS_FILE="$2"' in text
    assert 'FILTER_PACKAGES_FILE=""' in text and 'SERVED_NVRS_FILE=""' in text, (
        "unset defaults are the single-runner publisher's unchanged behavior"
    )


def test_chain_uses_the_shared_filter_helper_in_both_loops():
    text = CHAIN.read_text(encoding="utf-8")
    assert text.count('if _package_filtered_out "$pkg_path"; then') == 2, (
        "tier loop and stream loop must apply the same filter"
    )


def test_served_skip_reads_like_the_local_skip():
    text = SOURCES.read_text(encoding="utf-8")
    assert "already served by the published index" in text


def test_cell_runner_forwards_the_fanout_env():
    text = (ROOT / "scripts" / "run-package-factory-cell.sh").read_text(encoding="utf-8")
    assert "CHAIN_PACKAGES_FILE" in text and "CHAIN_SERVED_NVRS" in text


# --- workflow shape ----------------------------------------------------------

@pytest.fixture(scope="module")
def fanout():
    return yaml.safe_load(FANOUT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def band():
    return yaml.safe_load(BAND.read_text(encoding="utf-8"))


def test_publish_shares_the_one_publisher_group(fanout):
    conc = fanout["jobs"]["publish"]["concurrency"]
    assert conc["group"] == "publish-rpms"
    assert conc["cancel-in-progress"] is False, (
        "cancelling a RUNNING publisher mid-sync is how repos get wiped (#124)"
    )


def test_publish_runs_on_red_shards_but_not_cancelled_runs(fanout):
    cond = str(fanout["jobs"]["publish"].get("if", ""))
    assert "!cancelled()" in cond and "always()" not in cond


def test_bands_chain_in_order(fanout):
    for arch in ("x86", "arm"):
        for i in range(1, 5):
            needs = fanout["jobs"][f"band{i}-{arch}"]["needs"]
            assert f"band{i - 1}-{arch}" in needs, (
                "a band consumes its predecessor's collected output"
            )


def test_exactly_one_collector_writes_the_cumulative_artifact(band):
    writers = [
        name for name, job in band["jobs"].items()
        for step in job.get("steps", [])
        if "upload-artifact" in str(step.get("uses", ""))
        and "fanout-repo-" in str(step.get("with", {}).get("name", ""))
    ]
    assert writers == ["collect"], (
        f"cumulative artifact writers: {writers} -- two writers is the "
        "two-publishers-one-bucket shape at band scale"
    )


def test_shards_upload_even_when_red(band):
    uploads = [
        step for step in band["jobs"]["shard"]["steps"]
        if "upload-artifact" in str(step.get("uses", ""))
    ]
    assert len(uploads) == 1
    assert "always()" in str(uploads[0].get("if", "")), "#563 at shard scale"
    assert uploads[0]["with"]["if-no-files-found"] == "warn", (
        "an all-served shard has nothing new, and that is success"
    )


def test_shards_do_not_cancel_their_siblings(band):
    assert band["jobs"]["shard"]["strategy"]["fail-fast"] is False


def test_fanout_stays_on_free_hosted_runners(fanout):
    text = FANOUT.read_text(encoding="utf-8")
    assert "runs-on=" not in text, (
        "the fan-out exists to use FREE hosted concurrency; the AWS pool "
        "is the one-time bringup tool"
    )


def test_arm_bands_use_the_aarch64_image(fanout):
    for i in range(5):
        img = fanout["jobs"][f"band{i}-arm"]["with"]["image"]
        assert img.endswith("-aarch64"), (
            f"band{i}-arm: an amd64 image on an arm runner dies with "
            "'Exec format error' at the first rpmspec (maiden run, all 8 "
            "arm shards)"
        )


def test_bands_continue_past_red_shards(fanout):
    for arch in ("x86", "arm"):
        for i in range(1, 5):
            cond = str(fanout["jobs"][f"band{i}-{arch}"].get("if", ""))
            assert "!cancelled()" in cond, (
                f"band{i}-{arch}: default gating skips the rest of the chain "
                "after one red shard, stranding the collected band"
            )


# --- the single-runner publisher must skip served packages too ---------------

PUBLISHER = ROOT / ".github" / "workflows" / "publish-build-chain-rpms.yml"


@pytest.fixture(scope="module")
def publisher():
    return yaml.safe_load(PUBLISHER.read_text(encoding="utf-8"))


def test_publisher_lists_the_served_index(publisher):
    steps = publisher["jobs"]["build"]["steps"]
    lister = [s for s in steps if "list-served-nvrs.py" in str(s.get("run", ""))]
    assert lister, (
        "leg 33113528651 burned its whole 4.5h budget rebuilding served "
        "packages and republished a byte-identical index: without this the "
        "publisher restarts at tier 0 on every mock-cfg merge (#544)"
    )


def test_publisher_feeds_the_served_list_to_the_chain(publisher):
    steps = publisher["jobs"]["build"]["steps"]
    build = [s for s in steps if s.get("id") == "build"]
    assert build, "the build step must keep its id"
    assert "CHAIN_SERVED_NVRS" in build[0]["env"], (
        "listing the served NVRs is useless unless the chain is told to read them"
    )


def test_the_served_list_is_per_cell(publisher):
    text = PUBLISHER.read_text(encoding="utf-8")
    assert "/tmp/served-nvrs-${{ matrix.id }}.txt" in text, (
        "both arches share a runner-local /tmp in the matrix; one filename "
        "would let x86_64's list decide what aarch64 skips"
    )
