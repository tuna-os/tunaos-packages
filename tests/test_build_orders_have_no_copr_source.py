"""No root build order sources a package from a COPR (#391, #439).

A copr_name entry meant "already registered in the jreilly1821/c10s-gnome-*
COPR; the GitHub chain skips it". The buildroot then took the package from
whatever repository happened to carry it, and the factory never owned it.
Every entry of a root build-order*.yml is now either built here (a path) or
left to the buildroot's system repositories (not listed at all), so the
catalog carries no COPR-sourced rpm payload.

.copr/build-order-gnome49.yml is out of scope: it is the legacy GNOME 49
COPR manifest, and GNOME 49 is discontinued (#673).
"""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "manifests" / "catalog.yaml"


def test_root_build_orders_carry_no_copr_name() -> None:
    offenders = []
    for order in sorted(ROOT.glob("build-order*.yml")):
        data = yaml.safe_load(order.read_text(encoding="utf-8")) or {}
        for tier in data.get("tiers") or []:
            for pkg in tier.get("packages") or []:
                if "copr_name" in pkg:
                    offenders.append(f"{order.name}: {tier['name']}: {pkg}")
    assert not offenders, "COPR-sourced build-order entries:\n" + "\n".join(
        offenders
    )


def test_catalog_has_no_copr_payload() -> None:
    packages = (yaml.safe_load(CATALOG.read_text(encoding="utf-8")) or {})[
        "packages"
    ]
    copr = [
        f"{entry['name']} ({entry['family']})"
        for entry in packages
        if "copr" in (entry.get("packaging") or {}).get("rpm", {})
    ]
    assert not copr, "catalog entries still sourced from a COPR: " + ", ".join(
        copr
    )
