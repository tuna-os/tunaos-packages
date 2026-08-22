from __future__ import annotations

import importlib.util
import pathlib

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "planner", ROOT / "scripts" / "plan-package-factory.py"
)
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)


def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "manifests").mkdir()
    (tmp_path / "packages" / "demo").mkdir(parents=True)
    (tmp_path / "manifests" / "package-factory.yaml").write_text(
        "targets:\n"
        "  el10: {format: rpm, architectures: [x86_64], probe_image: example/el@sha256:abc}\n"
        "  debian: {format: deb, architectures: [amd64], probe_image: example/deb@sha256:def}\n"
        "dependency_catalog:\n"
        "  cmake: {el10: [cmake], debian: [cmake]}\n"
        "  rust: {el10: [rust], debian: [rustc]}\n"
    )
    (tmp_path / "manifests" / "package-builds.yaml").write_text(
        "native_builds:\n"
        "- {id: gnome, target: el10, architecture: x86_64, image: image, manifest: order.yml, mock_config: mock, source_paths: [src/gnome/]}\n"
    )
    (tmp_path / "packages" / "demo" / "package.yaml").write_text(
        "name: demo\nci: {canary: true}\ndependencies: {build: {capabilities: [cmake]}}\ntargets: [el10, debian]\n"
    )
    (tmp_path / "order.yml").write_text("tiers: []\n")
    return tmp_path


def test_recipe_change_selects_only_that_recipes_targets(tmp_path):
    root = repo(tmp_path)
    cells = planner.all_cells(root)
    selected = planner.select_cells(cells, ["packages/demo/package.yaml"])
    assert {(cell["package"], cell["target"]) for cell in selected} == {
        ("demo", "el10"), ("demo", "debian")
    }


def test_debian_renderer_change_does_not_select_rpm_or_native(tmp_path):
    root = repo(tmp_path)
    selected = planner.select_cells(planner.all_cells(root), ["scripts/assemble-deb-source-tree.py"])
    assert {cell["format"] for cell in selected} == {"deb"}


def test_native_build_chain_change_does_not_select_tideforge_rpm(tmp_path):
    root = repo(tmp_path)
    selected = planner.select_cells(planner.all_cells(root), ["scripts/build-chain.sh"])
    assert [cell["engine"] for cell in selected] == ["build-chain"]


def test_native_source_change_selects_only_its_queue(tmp_path):
    root = repo(tmp_path)
    selected = planner.select_cells(planner.all_cells(root), ["src/gnome/mutter/mutter.spec"])
    assert [cell["id"] for cell in selected] == ["gnome"]


def test_unrelated_change_selects_nothing(tmp_path):
    root = repo(tmp_path)
    assert planner.select_cells(planner.all_cells(root), ["README.md"]) == []


def test_factory_contract_change_fails_toward_building_everything(tmp_path):
    root = repo(tmp_path)
    cells = planner.all_cells(root)
    assert planner.select_cells(cells, ["manifests/package-factory.yaml"]) == cells


def test_factory_contract_change_selects_only_changed_target(tmp_path):
    root = repo(tmp_path)
    cells = planner.all_cells(root)
    selected = planner.select_cells(
        cells,
        ["manifests/package-factory.yaml"],
        changed_targets={"debian"},
    )
    assert {cell["target"] for cell in selected} == {"debian"}


def test_native_registry_change_selects_only_changed_row(tmp_path):
    root = repo(tmp_path)
    cells = planner.all_cells(root)
    selected = planner.select_cells(
        cells,
        ["manifests/package-builds.yaml"],
        changed_native_ids={"gnome"},
    )
    assert [cell["id"] for cell in selected] == ["gnome"]


def test_dependency_catalog_change_selects_only_consumers_and_target(tmp_path):
    root = repo(tmp_path)
    cells = planner.all_cells(root)
    selected = planner.select_cells(
        cells,
        ["manifests/package-factory.yaml"],
        changed_targets=set(),
        changed_capabilities={("cmake", "debian")},
    )
    assert [(cell["package"], cell["target"]) for cell in selected] == [("demo", "debian")]


def test_unused_dependency_catalog_change_selects_nothing(tmp_path):
    root = repo(tmp_path)
    selected = planner.select_cells(
        planner.all_cells(root),
        ["manifests/package-factory.yaml"],
        changed_targets=set(),
        changed_capabilities={("rust", "el10")},
    )
    assert selected == []


def test_every_matrix_row_has_the_same_schema(tmp_path):
    cells = planner.all_cells(repo(tmp_path))
    assert len({tuple(sorted(cell)) for cell in cells}) == 1


def test_selector_is_data_driven(tmp_path):
    cells = planner.all_cells(repo(tmp_path))
    assert {cell["target"] for cell in planner.select_by(cells, "target=debian")} == {"debian"}
    assert [cell["id"] for cell in planner.select_by(cells, "family=native")] == ["gnome"]
    assert {cell["engine"] for cell in planner.select_by(cells, "track=stable")} == {"tideforge", "build-chain"}


def test_shared_executor_pr_uses_contract_canaries(tmp_path):
    cells = planner.all_cells(repo(tmp_path))
    selected = planner.select_cells(
        cells,
        ["scripts/run-package-factory-cell.sh"],
        canary_common=True,
    )
    coordinates = {
        (cell["engine"], cell["target"], cell["format"], cell["architecture"])
        for cell in cells
    }
    assert len(selected) == len(coordinates)
    assert {
        (cell["engine"], cell["target"], cell["format"], cell["architecture"])
        for cell in selected
    } == coordinates


def test_explicit_canary_wins_over_alphabetical_order(tmp_path):
    root = repo(tmp_path)
    other = root / "packages" / "aaa" / "package.yaml"
    other.parent.mkdir()
    other.write_text("name: aaa\ntargets: [el10, debian]\n")
    selected = planner.canary_cells(planner.all_cells(root))
    tideforge = [cell for cell in selected if cell["engine"] == "tideforge"]
    assert {cell["package"] for cell in tideforge} == {"demo"}


def test_native_canary_scope_has_a_distinct_identity(tmp_path):
    root = repo(tmp_path)
    registry = root / "manifests" / "package-builds.yaml"
    registry.write_text(registry.read_text().replace("source_paths: [src/gnome/]", "source_paths: [src/gnome/], canary_tiers: base"))
    selected = planner.canary_cells(planner.all_cells(root))
    native = next(cell for cell in selected if cell["engine"] == "build-chain")
    assert native["id"] == "gnome-canary"
    assert native["tiers"] == "base"


def test_shared_executor_dominates_a_simultaneous_target_contract_change(tmp_path):
    cells = planner.all_cells(repo(tmp_path))
    selected = planner.select_cells(
        cells,
        ["scripts/run-package-factory-cell.sh", "manifests/package-factory.yaml"],
        changed_targets={"debian"},
        canary_common=True,
    )
    assert {cell["target"] for cell in selected} == {"el10", "debian"}


def test_published_index_reproves_wherever_a_buildroot_reads_it():
    """This assertion USED to say the opposite for deb, and the reversal is
    the point.

    published_index was treated as measurement-only for deb because the deb
    pipeline did not read it — run 32397627179 is the incident behind that:
    declaring a served apt index re-planned every deb cell and the change was
    blocked by the family's pre-existing breakage.

    But deb ignored the index because of a GAP, not by design: the deb
    container was never passed PUBLISHED_INDEX, so any recipe whose
    BuildRequires are themselves factory-built was unsatisfiable. #476 closed
    that. The index is now a real build input for deb, so changing it MUST
    re-prove those cells — reusing a deb built against a different package
    universe is exactly the silent failure the contract exists to prevent.

    The cost named in the old docstring is real and returns: a deb index
    change now re-plans deb cells, and a broken family blocks it. That is the
    correct trade once the index can change what gets built.

    Arch is unchanged: pkg.tar.zst still has no equivalent source.
    """
    for fmt, url in (("deb", "https://repo.example/tideforge/ubuntu/"),
                     ("rpm", "https://repo.example/repo/10/x86_64/")):
        old = {"format": fmt}
        new = {"format": fmt, "published_index": {"amd64": url}}
        assert planner._pipeline_view(old) != planner._pipeline_view(new), fmt

    old_arch = {"format": "pkg.tar.zst"}
    new_arch = {"format": "pkg.tar.zst",
                "published_index": {"x86_64": "https://repo.example/pacman/"}}
    assert planner._pipeline_view(old_arch) == planner._pipeline_view(new_arch)
