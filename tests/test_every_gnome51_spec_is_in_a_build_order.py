"""Every src/gnome-51/* spec must appear in a build-order manifest or be explicitly unwired.

When adding, forking, or updating a spec under src/gnome-51/, it is easy to assume
that running build-chain.sh against a manifest (such as build-order-gnome51.yml or
build-order-hummingbird-desktops.yml) will build and verify the package.

However, build-order-hummingbird-desktops.yml is generated dynamically by
scripts/measure-target-gap.py and only includes runtime gap packages (packages not
already shipped by the hummingbird base image). If a package is already shipped by
the base image (e.g. src/gnome-51/orca, as discovered in #598 / #599), it will not
appear in the hummingbird manifest tiers.

If a package is omitted from all build-order*.yml manifests:
  ./scripts/build-chain.sh --package src/gnome-51/<pkg> --manifest ...
matches zero tiers and silently exits with "All packages built successfully!" having
built nothing for that package.

To prevent silent no-op verification traps, this test enforces that every spec
directory in src/gnome-51/ either:
  1. appears as a `path:` entry in at least one build-order*.yml manifest, or
  2. is explicitly listed in UNWIRED_GNOME51_SPECS with documented rationale.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

# Packages intentionally present in src/gnome-51/ but not wired into any
# build-order*.yml manifest. Every entry must map the package path to a
# non-empty explanation of why it is unwired.
UNWIRED_GNOME51_SPECS: dict[str, str] = {
    "src/gnome-51/orca": (
        "Already shipped by the hummingbird base image (not a gap in "
        "build-order-hummingbird-desktops.yml) and not yet wired into "
        "build-order-gnome51.yml; spec is maintained (#598) for future builds."
    ),
}


def gnome51_spec_dirs() -> list[Path]:
    """All package directories under src/gnome-51/ containing at least one .spec file."""
    base = ROOT / "src" / "gnome-51"
    if not base.is_dir():
        return []
    return sorted(
        d for d in base.iterdir()
        if d.is_dir() and any(d.glob("*.spec"))
    )


def build_order_manifests() -> list[Path]:
    """All build-order YAML manifests in the repository."""
    return sorted(
        set(ROOT.glob("build-order*.yml")).union(ROOT.glob(".copr/build-order*.yml"))
    )


def referenced_paths() -> set[str]:
    """Every `path:` referenced across all build-order manifests."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            path = node.get("path")
            if isinstance(path, str):
                found.add(path.rstrip("/"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for manifest in build_order_manifests():
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        walk(data)

    return found


def validate_allowlist(allowlist: dict[str, str]) -> None:
    """Validate that every allowlist entry refers to a real spec dir, has a rationale, and is not wired."""
    all_wired = referenced_paths()
    for path_str, reason in allowlist.items():
        assert reason.strip(), f"Allowlist entry {path_str} must have a non-empty rationale"
        spec_dir = ROOT / path_str
        assert spec_dir.is_dir(), f"Allowlisted path {path_str} is not a directory in {ROOT}"
        assert any(spec_dir.glob("*.spec")), f"Allowlisted directory {path_str} contains no .spec files"
        assert path_str not in all_wired, (
            f"Allowlisted package {path_str} is now wired into a build-order manifest; "
            "remove it from UNWIRED_GNOME51_SPECS."
        )


def test_gnome51_spec_directories_exist() -> None:
    """Ensure discovery is active and not trivial/empty."""
    dirs = gnome51_spec_dirs()
    assert len(dirs) >= 20, f"Expected at least 20 spec directories in src/gnome-51, found {len(dirs)}"


def test_build_orders_are_discovered() -> None:
    """Ensure build-order manifests are discovered and parsed."""
    manifests = build_order_manifests()
    assert manifests, "No build-order manifests found"
    paths = referenced_paths()
    assert any("gnome-51" in p for p in paths), "No gnome-51 paths found in build-order manifests"


def test_unwired_allowlist_entries_are_valid_and_documented() -> None:
    """Every allowlist entry in UNWIRED_GNOME51_SPECS must be valid and documented."""
    validate_allowlist(UNWIRED_GNOME51_SPECS)


@pytest.mark.parametrize(
    "spec_dir",
    gnome51_spec_dirs(),
    ids=lambda p: p.name,
)
def test_every_gnome51_spec_is_in_a_build_order_or_allowlist(spec_dir: Path) -> None:
    rel_path = spec_dir.relative_to(ROOT).as_posix()
    all_wired = referenced_paths()

    if rel_path in all_wired:
        return

    assert rel_path in UNWIRED_GNOME51_SPECS, (
        f"{rel_path} contains a .spec file but is not referenced by any build-order*.yml "
        "manifest in this repository, nor listed in UNWIRED_GNOME51_SPECS. "
        "Either add it as a `path:` entry to the appropriate build-order manifest, "
        "or explicitly add it to UNWIRED_GNOME51_SPECS in "
        f"{Path(__file__).relative_to(ROOT)} with a documented rationale."
    )


def test_the_check_catches_an_unwired_unlisted_package() -> None:
    """A synthetic unwired spec directory not in the allowlist must fail the check."""
    fake_pkg = ROOT / "src" / "gnome-51" / "_test_fake_unwired_pkg"
    try:
        fake_pkg.mkdir(exist_ok=True)
        (fake_pkg / "fake.spec").write_text("Name: fake\nVersion: 1\nRelease: 1\n", encoding="utf-8")

        with pytest.raises(AssertionError, match=r"contains a \.spec file but is not referenced"):
            test_every_gnome51_spec_is_in_a_build_order_or_allowlist(fake_pkg)
    finally:
        if fake_pkg.exists():
            for child in fake_pkg.iterdir():
                child.unlink()
            fake_pkg.rmdir()


def test_empty_rationale_in_allowlist_is_rejected() -> None:
    """Every allowlist entry must have non-empty rationale."""
    with pytest.raises(AssertionError, match=r"must have a non-empty rationale"):
        validate_allowlist({"src/gnome-51/orca": "   "})


def test_wired_package_in_allowlist_is_rejected() -> None:
    """Allowlisting a package that is actually wired into a build-order manifest is an error."""
    with pytest.raises(AssertionError, match=r"is now wired into a build-order manifest"):
        validate_allowlist({
            "src/gnome-51/orca": UNWIRED_GNOME51_SPECS["src/gnome-51/orca"],
            "src/gnome-51/gtk4": "already wired",
        })


def test_nonexistent_directory_in_allowlist_is_rejected() -> None:
    """Allowlisting a non-existent directory must fail."""
    with pytest.raises(AssertionError, match=r"is not a directory"):
        validate_allowlist({"src/gnome-51/does_not_exist": "fake package"})
