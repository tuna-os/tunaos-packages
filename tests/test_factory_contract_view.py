"""One definition of "can this contract change a build", shared by two readers.

#473: scripts/tideforge-action-cache.py hashed the target's ENTIRE dict into
each cell's action key, so editing a bucket write path or a reporting label
rebuilt every cell on that target from scratch. The planner already had a
partial version of the same idea — published_index stripped for deb and
pkg.tar.zst — and the two could drift.

That is the shape #471 fixed for published_index itself: two hand-copied
readers, both wrong the same way, only one of which would ever have been
fixed. So the tests here pin the SHARING as much as the values.

The distinction that has to stay sharp: published_index looks like
publishing metadata and is inert for deb and Arch, but an rpm buildroot adds
it as a repo (run-package-factory-cell.sh writes it into /etc/yum.repos.d),
so for rpm it is a live build input. Getting that backwards would let a cell
reuse output built against a different package universe.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import factory_contract as fc  # noqa: E402

MANIFEST = ROOT / "manifests" / "package-factory.yaml"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tac = _load("tideforge_action_cache", "tideforge-action-cache.py")
planner = _load("plan_package_factory", "plan-package-factory.py")


# ------------------------------------------------------------------ the view


def test_write_paths_and_reporting_labels_are_inert():
    target = {
        "format": "rpm",
        "buildroot": "epel-10",
        "r2_path": "rpm/el10/{arch}",
        "r2_path_aarch64": "hummingbird/20251124-aarch64",
        "status": "supported",
        "gap_measurement": {"roots_manifest": "manifests/x.yaml"},
    }
    view = fc.build_view(target)
    assert view == {"format": "rpm", "buildroot": "epel-10"}


def test_published_index_is_live_wherever_a_buildroot_adds_it_as_a_source():
    """The one field where the answer depends on the format.

    rpm and deb both add it: run-package-factory-cell.sh writes
    /etc/yum.repos.d/tunaos-published.repo for rpm and
    /etc/apt/sources.list.d/tunaos-published-N.list for deb. Arch has no
    equivalent and resolves everything from the distro.
    """
    url = {"x86_64": "https://repo.example/"}
    for fmt in ("rpm", "deb"):
        assert fc.build_view({"format": fmt, "published_index": url}) == {
            "format": fmt, "published_index": url,
        }, fmt
    assert fc.build_view({"format": "pkg.tar.zst", "published_index": url}) == {
        "format": "pkg.tar.zst",
    }


def test_deb_rekeys_on_a_changed_index():
    """The reason deb moved out of the inert set (#476).

    deb used to be listed inert on the reading that published_index was
    publishing metadata. It was not consumed because of a GAP -- the deb
    container was never passed PUBLISHED_INDEX -- so a recipe whose
    BuildRequires are themselves factory-built could not resolve them at all.
    Now that the deb buildroot adds the index, two deb builds against
    DIFFERENT package universes must not share an action key.
    """
    a = fc.build_view({"format": "deb", "published_index": {"amd64": "https://a/"}})
    b = fc.build_view({"format": "deb", "published_index": {"amd64": "https://b/"}})
    assert a != b


def test_arch_still_does_not_rekey_on_the_index():
    """Guards the other direction: pkg.tar.zst genuinely does not read it, and
    re-keying on it would rebuild every Arch cell for nothing."""
    a = fc.build_view({"format": "pkg.tar.zst", "published_index": {"x86_64": "https://a/"}})
    b = fc.build_view({"format": "pkg.tar.zst", "published_index": {"x86_64": "https://b/"}})
    assert a == b


def test_a_malformed_contract_passes_through_untouched():
    """It must reach the caller that validates it, not become an empty dict
    here — a silently-normalised contract is how a cell builds against
    something nobody declared."""
    assert fc.build_view("not-a-mapping") == "not-a-mapping"
    assert fc.build_view(None) is None


def test_an_unknown_format_still_drops_the_universal_keys():
    assert fc.build_view({"format": "nix", "r2_path": "x"}) == {"format": "nix"}


# ------------------------------------------------------- both readers use it


def test_the_planner_and_the_action_key_share_one_view():
    """Not "both have a table with the same contents" — the same function."""
    assert planner._pipeline_view is fc.build_view
    assert "factory_contract" in (ROOT / "scripts" / "tideforge-action-cache.py").read_text()


def test_no_reader_keeps_a_private_inert_table():
    """A second table is how the two answers drifted in the first place."""
    for filename in ("plan-package-factory.py", "tideforge-action-cache.py"):
        text = (ROOT / "scripts" / filename).read_text()
        assert "PIPELINE_INERT_KEYS" not in text, filename


# ------------------------------------------------- the key actually reflects it


def _key(tmp_path, factory: dict) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "package-factory.yaml"
    manifest.write_text(yaml.safe_dump(factory), encoding="utf-8")
    recipe_dir = tmp_path / "packages" / "demo"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    (recipe_dir / "package.yaml").write_text("name: demo\n", encoding="utf-8")
    for relative in tac.COMMON_RENDERERS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("# renderer\n", encoding="utf-8")

    class Args:
        root = str(tmp_path)
        recipe = str(recipe_dir / "package.yaml")
        factory = str(manifest)
        target = "el10"
        arch = "x86_64"
        image = "example.test/build@sha256:" + "0" * 64
        source_date_epoch = "1755000000"
        dependency_key: list[str] = []

    return tac.digest_json(tac.action_inputs(Args()))


def _factory(**overrides):
    target = {
        "format": "rpm",
        "architectures": ["x86_64"],
        "buildroot": "epel-10",
        "r2_path": "rpm/el10/{arch}",
        "status": "supported",
    }
    target.update(overrides)
    return {"targets": {"el10": target}}


def test_changing_a_write_path_does_not_rebuild_anything(tmp_path):
    """The defect: this used to change every cell's key on the target."""
    before = _key(tmp_path / "a", _factory())
    after = _key(tmp_path / "b", _factory(r2_path="rpm/el10-moved/{arch}"))
    assert before == after


def test_changing_a_reporting_label_does_not_rebuild_anything(tmp_path):
    before = _key(tmp_path / "a", _factory())
    after = _key(tmp_path / "b", _factory(status="scaffold"))
    assert before == after


def test_changing_the_rpm_buildroots_index_still_rebuilds(tmp_path):
    """The other direction, and the one that must NOT be optimised away: an
    rpm buildroot resolves BuildRequires from published_index, so its output
    can legitimately differ when the index changes."""
    before = _key(tmp_path / "a", _factory(
        published_index={"x86_64": ["https://repo.example/one/"]}))
    after = _key(tmp_path / "b", _factory(
        published_index={"x86_64": ["https://repo.example/one/",
                                    "https://repo.example/two/"]}))
    assert before != after


def test_changing_a_real_build_input_still_rebuilds(tmp_path):
    before = _key(tmp_path / "a", _factory())
    after = _key(tmp_path / "b", _factory(buildroot="epel-11"))
    assert before != after


# ------------------------------------------------------------ the real manifest


def test_every_declared_target_keeps_its_build_inputs():
    """Whatever the view drops, it must never drop something a build reads."""
    factory = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    live = {"format", "architectures", "buildroot", "suites", "probe_image",
            "build_repositories", "system_repositories", "repository"}
    for target_id, spec in factory["targets"].items():
        view = fc.build_view(spec)
        for key in live & set(spec):
            assert key in view, (target_id, key)
        if spec.get("format") == "rpm" and "published_index" in spec:
            assert "published_index" in view, target_id
