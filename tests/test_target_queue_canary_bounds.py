"""Queue policy changes retain each family without twelve full PR chains."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("queue_planner", ROOT / "scripts/plan-package-factory.py")
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)
QUEUE = "manifests/target-queues/gnome.yaml"


def native_gnome():
    return [cell for cell in planner.native_cells(ROOT) if cell["family"] in {"gnome50", "gnome51"}]


def test_queue_only_pr_bounds_every_native_family_without_collapsing_architectures():
    cells = native_gnome()
    selected = planner.select_cells(cells, [QUEUE], canary_common=True)
    assert len(selected) == len(cells) == 12
    assert {cell["id"].removesuffix("-canary") for cell in selected} == {cell["id"] for cell in cells}
    assert all(cell["tiers"] == cell["canary_tiers"] == "base-tools" for cell in selected)
    assert all(not cell["tiers"] for cell in cells), "selection must not mutate its input"


def test_mixed_queue_and_manifest_diff_keeps_real_owners_full():
    selected = planner.select_cells(
        native_gnome(), [QUEUE, "build-order-gnome51.yml"], canary_common=True,
    )
    assert len(selected) == 12
    assert all(not cell["tiers"] for cell in selected if cell["family"] == "gnome51")
    assert all(cell["tiers"] == "base-tools" for cell in selected if cell["family"] == "gnome50")


def test_semantic_queue_filter_cannot_drop_an_independent_source_change():
    selected = planner.select_cells(
        native_gnome(), [QUEUE, "src/gnome-51/orca/orca.spec"],
        changed_graph_ids=set(), canary_common=True,
    )
    assert len(selected) == 6
    assert all(cell["family"] == "gnome51" and not cell["tiers"] for cell in selected)


def test_full_queue_runs_and_dependency_graph_changes_stay_full():
    for paths, canary in [([QUEUE], False), ([QUEUE, "manifests/dependency-trees/gnome.yaml"], True)]:
        selected = planner.select_cells(native_gnome(), paths, canary_common=canary)
        assert len(selected) == 12
        assert all(not cell["tiers"] for cell in selected)


def test_queue_only_cli_has_no_full_chain_continuations(tmp_path):
    changed = tmp_path / "changed.txt"
    changed.write_text(QUEUE + "\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/plan-package-factory.py"),
         "--root", str(ROOT), "--changed-files", str(changed), "--canary-common"],
        text=True, capture_output=True, check=True,
    )
    report = json.loads(result.stdout)
    rows = [row for matrix in report["matrices"] for row in json.loads(matrix)["include"]]
    assert report["selection_count"] == len(rows) == 12
    assert all(row["tiers"] == "base-tools" for row in rows)
    assert not any(row["id"].endswith(("-c1", "-c2")) for row in rows)
