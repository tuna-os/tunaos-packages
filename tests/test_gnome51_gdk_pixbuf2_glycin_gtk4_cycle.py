"""gdk-pixbuf2 -> glycin -> gtk4 -> gdk-pixbuf2 build cycle in GNOME 51 (#541).

gdk-pixbuf2 requires glycin for JXL/HEIF/AVIF/WebP/SVG support.
glycin requires gtk4.
gtk4 requires gdk-pixbuf2.

This cycle must be broken with a bootstrap pass:
1. gdk-pixbuf2-bootstrap.spec builds early with glycin disabled.
2. gtk4 builds against the bootstrap gdk-pixbuf2.
3. glycin builds against gtk4.
4. gdk-pixbuf2 (full) rebuilds with glycin enabled.
5. Downstream consumers (mutter, gnome-shell, nautilus) build against the full gdk-pixbuf2.
"""
from __future__ import annotations

import re
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
ORDER_FILE = ROOT / "build-order-gnome51.yml"
SPEC_DIR = ROOT / "src" / "deps" / "gdk-pixbuf2"


def get_tier_indices() -> tuple[int, int, int, int]:
    order = yaml.safe_load(ORDER_FILE.read_text(encoding="utf-8"))
    boot_tier = None
    gtk4_tier = None
    glycin_tier = None
    full_tier = None

    for i, tier in enumerate(order.get("tiers", [])):
        for pkg in tier.get("packages", []):
            path = pkg.get("path", "")
            if "gdk-pixbuf2" in path:
                if pkg.get("spec_override") == "gdk-pixbuf2-bootstrap.spec":
                    boot_tier = i
                elif not pkg.get("spec_override"):
                    full_tier = i
            elif "gtk4" in path:
                gtk4_tier = i
            elif "glycin" in path:
                glycin_tier = i

    assert boot_tier is not None, "gdk-pixbuf2-bootstrap not found in build-order-gnome51.yml"
    assert gtk4_tier is not None, "gtk4 not found in build-order-gnome51.yml"
    assert glycin_tier is not None, "glycin not found in build-order-gnome51.yml"
    assert full_tier is not None, "full gdk-pixbuf2 not found in build-order-gnome51.yml"

    return boot_tier, gtk4_tier, glycin_tier, full_tier


def test_tier_ordering_breaks_cycle():
    boot_tier, gtk4_tier, glycin_tier, full_tier = get_tier_indices()

    assert boot_tier < gtk4_tier, (
        f"gdk-pixbuf2 bootstrap (tier {boot_tier}) must precede gtk4 (tier {gtk4_tier})"
    )
    assert gtk4_tier < glycin_tier, (
        f"gtk4 (tier {gtk4_tier}) must precede glycin (tier {glycin_tier})"
    )
    assert glycin_tier < full_tier, (
        f"glycin (tier {glycin_tier}) must precede full gdk-pixbuf2 (tier {full_tier})"
    )


def test_full_gdk_pixbuf2_precedes_mutter_and_nautilus():
    order = yaml.safe_load(ORDER_FILE.read_text(encoding="utf-8"))
    _, _, _, full_tier = get_tier_indices()

    for i, tier in enumerate(order.get("tiers", [])):
        for pkg in tier.get("packages", []):
            path = pkg.get("path", "")
            if "mutter" in path or "nautilus" in path:
                assert full_tier < i, (
                    f"full gdk-pixbuf2 (tier {full_tier}) must precede {path} (tier {i})"
                )


def test_spec_files_have_bcond_glycin():
    full_spec = (SPEC_DIR / "gdk-pixbuf2.spec").read_text(encoding="utf-8")
    boot_spec = (SPEC_DIR / "gdk-pixbuf2-bootstrap.spec").read_text(encoding="utf-8")

    assert re.search(r"^%bcond\s+glycin\s+1", full_spec, re.M), (
        "gdk-pixbuf2.spec must enable glycin by default"
    )
    assert re.search(r"^%bcond\s+glycin\s+0", boot_spec, re.M), (
        "gdk-pixbuf2-bootstrap.spec must disable glycin by default"
    )

    assert "%if %{with glycin}" in full_spec
    assert "%if %{with glycin}" in boot_spec
