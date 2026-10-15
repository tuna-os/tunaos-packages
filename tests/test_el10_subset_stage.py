"""EL10-only candidate subset; the six-cell stager stays whole."""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stager = _load("stager", "scripts/stage-tuna-desktop-el10.py")
closure = _load("closure", "scripts/el10-rpm-closure.py")

# Production el10 probe (@ main): tag form, so the stager's singular
# probe_image branch applies and base_reference carries the tag.
PROBE = "quay.io/centos/centos:stream10"
PINNED = "quay.io/centos/centos@sha256:" + "ab" * 32
PLATFORM = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}
SHA = "c" * 40

# Exact Alma probe pins (fat-lto target definitions); Alma x86_64 uses
# the adapter platform spelling linux/amd64/v2.
ALMA_PROBE = {
    ("alma10", "x86_64"): (
        "quay.io/almalinuxorg/almalinux@sha256:"
        "51d5589de16b26f7145a20e2eba45aa2030fa12a2f9782b04fe5a4e8e6dc7912"
    ),
    ("alma10", "aarch64"): (
        "quay.io/almalinuxorg/almalinux@sha256:"
        "f65410c601aa3b514801aecf8d10eeefaa5b6a56248279b96e4085f1861b7d64"
    ),
    ("alma10-kitten", "x86_64"): (
        "quay.io/almalinuxorg/almalinux@sha256:"
        "a85da6343aca56df44ae45493a78bda56d4055d6e2d4c950ba81c7e25b25491a"
    ),
    ("alma10-kitten", "aarch64"): (
        "quay.io/almalinuxorg/almalinux@sha256:"
        "ea0cc0a1592d6ee73727e9efe5628094d0e4e12a762e26bce966592ad5d1ef6c"
    ),
}
ALMA_PLATFORM = {"x86_64": "linux/amd64/v2", "aarch64": "linux/arm64"}


def _tarball(path, members):
    with tarfile.open(path, "w:gz") as archive:
        for name, data in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))


def fixture(tmp_path, cells, factory_path=None):
    """Stage inputs against the REAL production recipe and factory.

    The recipe gains the PR818 portal/cursor prerequisites in-fixture
    (as the six-cell test does); the factory defaults to production
    byte-identical: the subset must pass where Alma definitions are
    absent.
    """
    recipe = yaml.safe_load((ROOT / "packages/tuna-desktop/package.yaml").read_text())
    recipe["install"]["files"] += [
        {"source": src, "destination": dest}
        for src, dest in stager.PORTAL_DESTINATIONS.items()
    ]
    recipe["install"]["directories"] = [
        {"source": stager.ICON_ROOT, "destination": "usr/share/tuna-desktop/icons"}
    ]
    recipe["files"]["common"] += [
        "usr/share/tuna-desktop/icons",
        *stager.PORTAL_DESTINATIONS.values(),
    ]
    recipe_path = tmp_path / "recipe.yaml"
    recipe_path.write_text(yaml.safe_dump(recipe))
    if factory_path is None:
        factory_path = ROOT / "manifests/package-factory.yaml"
    lock = b"version = 4\n"
    prefix = f"tuna-desktop-{SHA}"
    payload = {f"{prefix}/Cargo.lock": lock}
    payload.update({f"{prefix}/{e['source']}": b"fixture" for e in recipe["install"]["files"]})
    payload.update(
        {
            f"{prefix}/{stager.ICON_ROOT}/Adwaita/{name}": b"fixture"
            for name in [
                "LICENSE-MIT",
                "UPSTREAM-COPYING",
                "index.theme",
                "cursors_scalable/default/metadata.json",
                "cursors_scalable/default/default.svg",
            ]
        }
    )
    source_path, vendor_path = tmp_path / "source.tar.gz", tmp_path / "vendor.tar.gz"
    _tarball(source_path, payload)
    _tarball(vendor_path, {f"{prefix}/vendor/ok": b"ok"})

    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    lock_sha = hashlib.sha256(lock).hexdigest()
    inputs = {
        "source_sha": SHA,
        "source_archive_sha256": digest(source_path),
        "vendor_archive_sha256": digest(vendor_path),
        "lock_sha256": lock_sha,
        "vendor_receipt": {
            "source_sha": SHA,
            "lock_sha256": lock_sha,
            "archive_sha256": digest(vendor_path),
            "offline_metadata_success": True,
            "run_id": 1,
        },
        "vendor_url": (
            "https://github.com/tuna-os/tunaos-packages/releases/download/"
            "test/tuna-desktop-vendor.tar.gz"
        ),
        "cells": cells,
    }
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps(inputs))
    return recipe_path, factory_path, inputs_path, source_path, vendor_path


def el10_cells():
    return [
        {
            "consumer": "skipjack",
            "target": "el10",
            "arch": arch,
            "base_image": PINNED,
            "base_reference": PROBE,
            "platform": PLATFORM[arch],
        }
        for arch in ("x86_64", "aarch64")
    ]


def six_cells():
    cells = el10_cells()
    for consumer, target in (
        ("albacore", "alma10"),
        ("yellowfin", "alma10-kitten"),
    ):
        for arch in ("x86_64", "aarch64"):
            cells.append({"consumer": consumer, "target": target, "arch": arch})
    return cells


def full_six_cells():
    """Six cells with complete valid fields for the union factory."""
    cells = el10_cells()
    for consumer, target in (
        ("albacore", "alma10"),
        ("yellowfin", "alma10-kitten"),
    ):
        for arch in ("x86_64", "aarch64"):
            probe = ALMA_PROBE[(target, arch)]
            cells.append(
                {
                    "consumer": consumer,
                    "target": target,
                    "arch": arch,
                    "base_image": probe,
                    "base_reference": probe,
                    "platform": ALMA_PLATFORM[arch],
                }
            )
    return cells


def union_factory(tmp_path):
    """Production factory plus the exact fat-lto union blocks.

    Adapters, Alma targets, and catalog Alma entries are the exact
    fat-lto values; nothing is invented. Used only to prove the
    default six-cell path is preserved end to end.
    """
    factory = yaml.safe_load((ROOT / "manifests/package-factory.yaml").read_text())
    factory["consumer_adapters"] = {
        "albacore": {
            "target": "alma10",
            "manager": "dnf",
            "architectures": {"linux/amd64/v2": "x86_64", "linux/arm64": "aarch64"},
            "cpuBaselines": ["x86-64-v2", "armv8-a"],
        },
        "yellowfin": {
            "target": "alma10-kitten",
            "manager": "dnf",
            "architectures": {"linux/amd64/v2": "x86_64", "linux/arm64": "aarch64"},
            "cpuBaselines": ["x86-64-v2", "armv8-a"],
        },
        "skipjack": {
            "target": "el10",
            "manager": "dnf",
            "architectures": {"linux/amd64": "x86_64", "linux/arm64": "aarch64"},
            "cpuBaselines": ["x86-64-v3", "armv8-a"],
        },
    }
    for target, buildroot in (("alma10", "alma10"), ("alma10-kitten", "alma10-kitten")):
        factory["targets"][target] = {
            "status": "supported",
            "format": "rpm",
            "buildroot": buildroot,
            "architectures": ["x86_64", "aarch64"],
            "r2_path": f"rpm/{target}/{{arch}}",
            "repository": "rpm-md",
            "probe_images": {
                arch: ALMA_PROBE[(target, arch)] for arch in ("x86_64", "aarch64")
            },
            "platforms": dict(ALMA_PLATFORM),
            "cpu_baselines": {"x86_64": "x86-64-v2", "aarch64": "armv8-a"},
            "build_repositories": ["baseos", "appstream", "crb", "epel"],
            "system_repositories": ["baseos", "appstream", "crb", "epel"],
        }
    for cap in ("rust", "c-compiler", "pkg-config"):
        catalog = factory["dependency_catalog"][cap]
        catalog["alma10"] = list(catalog["el10"])
        catalog["alma10-kitten"] = list(catalog["el10"])
    path = tmp_path / "factory-union.yaml"
    path.write_text(yaml.safe_dump(factory))
    return path


def test_subset_cells_constant_bound():
    assert closure.SKIPJACK_EL10_CELLS == stager.EL10_ONLY_CELLS
    assert closure.ARCH_PLATFORMS == stager.ARCH_PLATFORMS


def test_subset_stages_two_el10_cells(tmp_path):
    paths = fixture(tmp_path, el10_cells())
    out = tmp_path / "out"
    receipt = stager.stage_el10_only(*paths, out)
    assert receipt["candidate_only"] is True
    assert receipt["release_qualified"] is False
    assert receipt["published"] is False
    assert receipt["cell_subset"] == ["skipjack"]
    assert {(c["consumer"], c["target"], c["arch"]) for c in receipt["cells"]} == set(
        stager.EL10_ONLY_CELLS
    )
    for arch in ("x86_64", "aarch64"):
        projected = yaml.safe_load(
            (out / f"skipjack-el10-{arch}" / "package.yaml").read_text()
        )
        assert projected["targets"] == ["el10"]
        assert list(projected["dependencies"]["build"]["targets"]) == ["el10"]
        assert list(projected["dependencies"]["runtime"]["targets"]) == ["el10"]
    by_arch = {c["arch"]: c for c in receipt["cells"]}
    assert by_arch["x86_64"]["cpu_baseline"] == "x86-64-v3"
    assert by_arch["aarch64"]["cpu_baseline"] == "armv8-a"
    assert by_arch["x86_64"]["build_requires"][:2] == ["rust", "cargo"]
    assert "clang-devel" in by_arch["x86_64"]["build_requires"]
    assert by_arch["x86_64"]["runtime_requires"] == stager.RUNTIME


def test_full_stager_still_requires_six_cells(tmp_path):
    paths = fixture(tmp_path, el10_cells())
    with pytest.raises(ValueError, match="six distinct"):
        stager.stage(*paths, tmp_path / "out")


def test_full_stager_default_receipt_has_no_subset_marker(tmp_path):
    paths = fixture(tmp_path, full_six_cells(), union_factory(tmp_path))
    receipt = stager.stage(*paths, tmp_path / "out")
    assert len(receipt["cells"]) == 6
    assert "cell_subset" not in receipt
    assert receipt["candidate_only"] is True
    assert receipt["release_qualified"] is False
    assert receipt["published"] is False


def test_full_stager_fails_adapters_on_production_factory(tmp_path):
    paths = fixture(tmp_path, six_cells())
    with pytest.raises(ValueError, match="wrong consumer adapter: skipjack/el10/x86_64"):
        stager.stage(*paths, tmp_path / "out")


def test_full_stager_fails_alma_target_with_skipjack_adapter(tmp_path):
    # Production factory plus the exact skipjack adapter (fat-lto,
    # consumer_adapters.skipjack): skipjack cells admit, then the first
    # Alma cell proves the missing native target definition.
    factory = yaml.safe_load((ROOT / "manifests/package-factory.yaml").read_text())
    factory["consumer_adapters"] = {
        "skipjack": {
            "target": "el10",
            "manager": "dnf",
            "architectures": {"linux/amd64": "x86_64", "linux/arm64": "aarch64"},
            "cpuBaselines": ["x86-64-v3", "armv8-a"],
        }
    }
    factory_path = tmp_path / "factory.yaml"
    factory_path.write_text(yaml.safe_dump(factory))
    recipe_path, _, inputs_path, source_path, vendor_path = fixture(tmp_path, six_cells())
    with pytest.raises(
        ValueError, match=r"missing supported native target: alma10/x86_64 \(PR819\)"
    ):
        stager.stage(
            recipe_path, factory_path, inputs_path, source_path, vendor_path, tmp_path / "out"
        )


def _subset_receipt(tmp_path):
    paths = fixture(tmp_path, el10_cells())
    return stager.stage_el10_only(*paths, tmp_path / "out")


def _clone(document):
    return json.loads(json.dumps(document))


def test_receipt_validator_accepts_subset_receipt(tmp_path):
    sha, cells = closure.validate_candidate_receipt(_subset_receipt(tmp_path))
    assert sha == SHA
    assert len(cells) == 2


def test_receipt_validator_rejects_partial_cells(tmp_path):
    receipt = _subset_receipt(tmp_path)
    single = _clone(receipt)
    single["cells"] = single["cells"][:1]
    with pytest.raises(ValueError, match="incomplete cell set"):
        closure.validate_candidate_receipt(single)
    empty = _clone(receipt)
    empty["cells"] = []
    with pytest.raises(ValueError, match="stages no cells"):
        closure.validate_candidate_receipt(empty)
    dupe = _clone(receipt)
    dupe["cells"] = [dupe["cells"][0], _clone(dupe["cells"][0])]
    with pytest.raises(ValueError, match="duplicate cell"):
        closure.validate_candidate_receipt(dupe)


def test_receipt_validator_rejects_coerced_schema_and_hashes(tmp_path):
    receipt = _subset_receipt(tmp_path)
    for bad_schema in (True, "1", 1.0, None):
        tampered = _clone(receipt)
        tampered["schema"] = bad_schema
        with pytest.raises(ValueError, match="schema must be the integer 1"):
            closure.validate_candidate_receipt(tampered)
    for key in ("source_sha", "source_archive_sha256", "vendor_archive_sha256", "lock_sha256"):
        for bad_hash in (12345, None, ["ab" * 32], True):
            tampered = _clone(receipt)
            tampered[key] = bad_hash
            with pytest.raises(ValueError):
                closure.validate_candidate_receipt(tampered)
    tampered = _clone(receipt)
    tampered["cells"][0]["recipe_sha256"] = "zz"
    with pytest.raises(ValueError, match="bad recipe hash"):
        closure.validate_candidate_receipt(tampered)


def test_receipt_validator_rejects_base_shape_drift(tmp_path):
    receipt = _subset_receipt(tmp_path)
    for bad_image in (
        "quay.io/centos/centos:stream10",
        "quay.io/centos/centos@sha256:xyz",
        "@sha256:" + "ab" * 32,
        "has space@sha256:" + "ab" * 32,
        42,
        None,
    ):
        tampered = _clone(receipt)
        tampered["cells"][0]["base_image"] = bad_image
        with pytest.raises(ValueError, match="base_image"):
            closure.validate_candidate_receipt(tampered)
    tampered = _clone(receipt)
    tampered["cells"][0]["base_reference"] = ""
    with pytest.raises(ValueError, match="base_reference missing"):
        closure.validate_candidate_receipt(tampered)
    tampered = _clone(receipt)
    del tampered["cells"][0]["base_reference"]
    with pytest.raises(ValueError, match="base_reference missing"):
        closure.validate_candidate_receipt(tampered)


def test_receipt_validator_rejects_platform_arch_mismatch(tmp_path):
    receipt = _subset_receipt(tmp_path)
    tampered = _clone(receipt)
    tampered["cells"][0]["platform"] = "linux/arm64"
    assert tampered["cells"][0]["arch"] == "x86_64"
    with pytest.raises(ValueError, match="does not match arch"):
        closure.validate_candidate_receipt(tampered)


def test_receipt_validator_rejects_relabel(tmp_path):
    receipt = _subset_receipt(tmp_path)
    relabeled = _clone(receipt)
    relabeled["cells"][0]["consumer"] = "albacore"
    relabeled["cells"][0]["target"] = "alma10"
    with pytest.raises(ValueError, match="outside EL10 subset"):
        closure.validate_candidate_receipt(relabeled)
    wrong_marker = _clone(receipt)
    wrong_marker["cell_subset"] = ["albacore"]
    with pytest.raises(ValueError, match="want \\['skipjack'\\]"):
        closure.validate_candidate_receipt(wrong_marker)


def test_receipt_validator_rejects_non_candidate(tmp_path):
    receipt = _subset_receipt(tmp_path)
    for key, value in (("published", True), ("release_qualified", True)):
        tampered = _clone(receipt)
        tampered[key] = value
        with pytest.raises(ValueError):
            closure.validate_candidate_receipt(tampered)
    tampered = _clone(receipt)
    tampered["source_sha"] = "zz"
    with pytest.raises(ValueError, match="exact source SHA"):
        closure.validate_candidate_receipt(tampered)


def _el10_cfg():
    factory = yaml.safe_load((ROOT / "manifests/package-factory.yaml").read_text())
    return factory["targets"]["el10"]


def test_closure_plan_uses_production_el10_repos(tmp_path):
    receipt = _subset_receipt(tmp_path)
    cfg = _el10_cfg()
    cell = next(c for c in receipt["cells"] if c["arch"] == "x86_64")
    plan = closure.plan_base_closure(
        cell, cfg, cell["build_requires"], cell["runtime_requires"]
    )
    assert plan["cell"] == "skipjack-el10-x86_64"
    assert "https://repo.tunaos.org/repo/10/x86_64/" in plan["repos"]["published_index"]
    assert "https://repo.tunaos.org/xfce/10-stream-x86_64/" in plan["repos"]["published_index"]
    assert "https://repo.tunaos.org/rpm/el10/aarch64/" not in plan["repos"]["published_index"]
    assert plan["repos"]["build"] == ["crb", "epel"]
    assert plan["repos"]["system"] == ["crb", "epel"]
    assert [s["name"] for s in plan["steps"]] == [
        "resolve-build-requires",
        "resolve-runtime-requires",
        "abi-closure",
    ]
    assert "clang-devel" in plan["requires"]["build"]
    arm = next(c for c in receipt["cells"] if c["arch"] == "aarch64")
    arm_plan = closure.plan_base_closure(arm, cfg, arm["build_requires"], arm["runtime_requires"])
    assert arm_plan["repos"]["published_index"] == ["https://repo.tunaos.org/rpm/el10/aarch64/"]


def test_closure_plan_rejects_foreign_cell(tmp_path):
    foreign = {"consumer": "albacore", "target": "alma10", "arch": "x86_64"}
    with pytest.raises(ValueError, match="outside EL10 subset"):
        closure.plan_base_closure(foreign, _el10_cfg(), ["a"], ["b"])


def test_closure_plan_rejects_malformed_requires_and_index(tmp_path):
    receipt = _subset_receipt(tmp_path)
    cfg = _el10_cfg()
    cell = next(c for c in receipt["cells"] if c["arch"] == "x86_64")
    good_build, good_runtime = cell["build_requires"], cell["runtime_requires"]
    for bad in ([], ["ok", "ok"], ["ok", ""], ["ok", 42], ["ok", None], "not-a-list"):
        with pytest.raises(ValueError):
            closure.plan_base_closure(cell, cfg, bad, good_runtime)
        with pytest.raises(ValueError):
            closure.plan_base_closure(cell, cfg, good_build, bad)
    no_arch = _clone(cfg)
    no_arch["published_index"] = {"aarch64": "https://repo.tunaos.org/rpm/el10/aarch64/"}
    with pytest.raises(ValueError, match="no served index for x86_64"):
        closure.plan_base_closure(cell, no_arch, good_build, good_runtime)
    empty_index = _clone(cfg)
    empty_index["published_index"] = {"x86_64": []}
    with pytest.raises(ValueError, match="non-empty URL list"):
        closure.plan_base_closure(cell, empty_index, good_build, good_runtime)
    bad_shape = _clone(cfg)
    bad_shape["published_index"] = 42
    with pytest.raises(ValueError, match="unusable shape"):
        closure.plan_base_closure(cell, bad_shape, good_build, good_runtime)
    deb_cfg = _clone(cfg)
    deb_cfg["format"] = "deb"
    with pytest.raises(ValueError, match="needs an rpm target"):
        closure.plan_base_closure(cell, deb_cfg, good_build, good_runtime)


def _draft_bundle(tmp_path):
    # Schema-shape fixture only. The producer identity is
    # fixture-minimal (run 1, matching the vendor receipt convention)
    # and claims NO measured producer, artifact, or signature.
    receipt = _subset_receipt(tmp_path)
    cfg = _el10_cfg()
    cell = next(c for c in receipt["cells"] if c["arch"] == "x86_64")
    plan = closure.plan_base_closure(
        cell, cfg, cell["build_requires"], cell["runtime_requires"]
    )
    report = {
        "cell": plan["cell"],
        "plan_sha256": closure.canonical_sha256(plan),
        "receipt_sha256": closure.canonical_sha256(receipt),
        "source_sha": SHA,
        "base_image": plan["base_image"],
        "producer": {
            "run_id": 1,
            "run_url": "https://github.com/tuna-os/tunaos-packages/actions/runs/1",
        },
        "signatures": {"signed_by": "tunaos-packages", "key": "repo-signing-2026"},
        "dependencies": {
            "rust": [{"evr": "1.92.0-1.el10", "status": "resolved"}],
            "clang-devel": [{"evr": "19.1.7-1.el10", "status": "resolved"}],
        },
        "covers_requires": {
            "build": list(plan["requires"]["build"]),
            "runtime": list(plan["requires"]["runtime"]),
        },
        "missing": [],
        "abi_conflicts": [],
        "packages": [
            {"name": "rust", "status": "resolved", "evr": "1.92.0-1.el10"},
            {"name": "clang-devel", "status": "resolved", "evr": "19.1.7-1.el10"},
        ],
    }
    return report, plan, receipt


def test_schema_check_accepts_well_formed_draft_report(tmp_path):
    report, plan, _ = _draft_bundle(tmp_path)
    assert closure.validate_closure_report(report, plan["cell"]) == 2


def test_schema_check_rejects_producer_and_signature_gaps(tmp_path):
    report, plan, _ = _draft_bundle(tmp_path)
    for bad_run_id in (True, 0, -3, "1", None):
        tampered = _clone(report)
        tampered["producer"]["run_id"] = bad_run_id
        with pytest.raises(ValueError, match="run_id"):
            closure.validate_closure_report(tampered, plan["cell"])
    for bad_url in (
        "https://github.com/other/repo/actions/runs/1",
        "not-a-url",
        None,
    ):
        tampered = _clone(report)
        tampered["producer"]["run_url"] = bad_url
        with pytest.raises(ValueError, match="run_url"):
            closure.validate_closure_report(tampered, plan["cell"])
    for bad_signatures in ({}, {"signed_by": "", "key": "k"}, {"signed_by": "x"}, None):
        tampered = _clone(report)
        tampered["signatures"] = bad_signatures
        with pytest.raises(ValueError, match="[Ss]ignatures"):
            closure.validate_closure_report(tampered, plan["cell"])


def test_schema_check_rejects_unmeasured_closure(tmp_path):
    report, plan, _ = _draft_bundle(tmp_path)
    bad_missing = _clone(report)
    bad_missing["missing"] = ["libcanberra-pulseaudio"]
    with pytest.raises(ValueError, match="unavailable packages"):
        closure.validate_closure_report(bad_missing, plan["cell"])
    bad_abi = _clone(report)
    bad_abi["abi_conflicts"] = ["gtk4 < 4.12"]
    with pytest.raises(ValueError, match="incompatible ABIs"):
        closure.validate_closure_report(bad_abi, plan["cell"])
    bad_status = _clone(report)
    bad_status["packages"] = [{"name": "rust", "status": "failed", "evr": "1.92.0-1.el10"}]
    with pytest.raises(ValueError, match="unresolved package"):
        closure.validate_closure_report(bad_status, plan["cell"])
    bad_evr = _clone(report)
    bad_evr["packages"] = [{"name": "rust", "status": "resolved", "evr": ""}]
    with pytest.raises(ValueError, match="lacks an EVR"):
        closure.validate_closure_report(bad_evr, plan["cell"])
    bad_deps = _clone(report)
    bad_deps["dependencies"] = {}
    with pytest.raises(ValueError, match="dependency observations"):
        closure.validate_closure_report(bad_deps, plan["cell"])
    bad_cell = _clone(report)
    bad_cell["cell"] = "skipjack-el10-aarch64"
    with pytest.raises(ValueError, match="another cell"):
        closure.validate_closure_report(bad_cell, plan["cell"])


# Measured admission is not implemented: every report is rejected,
# including self-consistent adversarial ones. Nothing here invents a
# measured EL10 green.


def test_admission_always_fails_complete_echo_two_packages(tmp_path):
    # covers_requires echoes the full plan, yet only two packages are
    # observed while the entire runtime require set is unobserved.
    report, plan, receipt = _draft_bundle(tmp_path)
    assert report["covers_requires"] == plan["requires"]
    assert len(report["packages"]) == 2
    with pytest.raises(ValueError, match="not implemented"):
        closure.admit_measured_closure(report, plan, receipt)


def test_admission_always_fails_wrong_run_suffix(tmp_path):
    # run_id 1 with a run_url suffix of 2: unbound producer identity.
    # Shape alone passes; admission still fails.
    report, plan, receipt = _draft_bundle(tmp_path)
    report["producer"]["run_url"] = (
        "https://github.com/tuna-os/tunaos-packages/actions/runs/2"
    )
    assert closure.validate_closure_report(report, plan["cell"]) == 2
    with pytest.raises(ValueError, match="not implemented"):
        closure.admit_measured_closure(report, plan, receipt)


def test_admission_always_fails_arbitrary_signatures(tmp_path):
    # Well-formed but arbitrary strings: no cryptographic verification,
    # no trusted head, no artifact binding.
    report, plan, receipt = _draft_bundle(tmp_path)
    report["signatures"] = {"signed_by": "anyone", "key": "untrusted"}
    assert closure.validate_closure_report(report, plan["cell"]) == 2
    with pytest.raises(ValueError, match="not implemented"):
        closure.admit_measured_closure(report, plan, receipt)


def test_admission_always_fails_invented_receipt_unbound_plan(tmp_path):
    # A self-consistent invented pair: the plan is rebuilt for invented
    # requires and the report rebinds to it, yet nothing measured it.
    report, plan, receipt = _draft_bundle(tmp_path)
    invented = _clone(plan)
    invented["requires"] = {"build": ["invented-build"], "runtime": ["invented-runtime"]}
    forged = _clone(report)
    forged["plan_sha256"] = closure.canonical_sha256(invented)
    forged["covers_requires"] = {
        "build": ["invented-build"],
        "runtime": ["invented-runtime"],
    }
    assert closure.validate_closure_report(forged, invented["cell"]) == 2
    with pytest.raises(ValueError, match="not implemented"):
        closure.admit_measured_closure(forged, invented, receipt)
