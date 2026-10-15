"""RFC 011 Phase 1: the generic gap command resolves target inputs centrally."""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "gap", ROOT / "scripts" / "gap_engine.py"
)
gap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gap)


def factory() -> dict:
    return yaml.safe_load(
        (ROOT / "manifests" / "package-factory.yaml").read_text(encoding="utf-8")
    )


def test_hummingbird_gap_contract_is_complete() -> None:
    measurement = gap.target_measurement(factory(), "hummingbird")
    assert measurement["roots_manifest"] == "manifests/hummingbird-desktops.yaml"
    assert measurement["target_index"].startswith("https://")
    assert measurement["reference_index"].startswith("https://")


def test_target_without_gap_contract_is_not_silently_measured() -> None:
    with pytest.raises(SystemExit, match="no gap_measurement contract"):
        gap.target_measurement(factory(), "el10")


def test_the_emitter_works_when_the_catalog_is_defaulted() -> None:
    """The generic entrypoint passes no --catalog, so args.catalog is None.

    main() resolves the real path into `catalog_path` (from the target's
    gap_measurement contract) right after argument parsing; everything
    downstream must read that, never args.catalog. The scheduled drift
    re-measure crashed on exactly this -- AttributeError on
    `args.catalog.resolve()` at the --build-order emit, after the report was
    already written -- so the drift PR carried a fresh JSON and a stale
    build order.
    """
    body = (ROOT / "scripts" / "gap_engine.py").read_text(
        encoding="utf-8"
    )
    after_default = body[body.index("catalog_path = args.catalog or "):]
    code = "\n".join(
        line for line in after_default.splitlines()
        if not line.lstrip().startswith("#")
    )
    uses = code.count("args.catalog")
    assert uses == 1, (
        "after `catalog_path` is resolved, nothing may dereference args.catalog: "
        "it is None whenever measure-target-gap.py --target supplied the catalog "
        f"(found {uses} uses, expected only the fallback itself)"
    )


def test_generic_entrypoint_is_checked_in() -> None:
    entrypoint = ROOT / "scripts" / "measure-target-gap.py"
    body = entrypoint.read_text(encoding="utf-8")
    assert "gap_engine.py" in body
    assert "_engine.main()" in body
    assert "requires --target" in body


def test_fedora_gap_contract_is_complete() -> None:
    measurement = gap.target_measurement(factory(), "fedora")
    assert measurement["roots_manifest"] == "manifests/xfce-fedora.yaml"
    assert measurement["target_index"].startswith("https://")
    assert measurement["reference_index"].startswith("https://")
    assert measurement["drift"]["mode"] == "exhibit"
    assert measurement["drift"]["build_order"] == "build-order-xfce-fedora.generated.yml"
    assert measurement["drift"]["compare_to"] == "build-order-xfce-fedora.yml"


def test_fedora_shadow_measurement_artifacts_exist() -> None:
    import json

    report_file = ROOT / "fedora-gap.json"
    order_file = ROOT / "build-order-xfce-fedora.generated.yml"
    assert report_file.is_file(), "fedora-gap.json must be committed beside the executed order"
    assert order_file.is_file(), "build-order-xfce-fedora.generated.yml must be committed"

    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["target"]["id"] == "fedora-44-x86_64"
    assert "xfce" in report["desktops"]
    assert report["desktops"]["xfce"]["roots"] == ["xfwl4"]

    order = yaml.safe_load(order_file.read_text(encoding="utf-8"))
    assert order["target"] == "fedora-44-x86_64"
    assert "tiers" in order

