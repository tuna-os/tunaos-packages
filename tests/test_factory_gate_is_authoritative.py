"""The unified factory has one required status context, not compatibility aliases."""
from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "package-factory.yml"


def test_package_factory_exposes_only_the_authoritative_gate() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "name: Package factory gate" in text
    for alias in (
        "compatibility-tideforge:",
        "compatibility-arch:",
        "compatibility-el10:",
    ):
        assert alias not in text


def test_legacy_gate_contexts_are_not_emitted() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    for legacy_context in (
        "name: Tideforge gate",
        "name: Tideforge gate (arch)",
        "name: el10-release-gate",
    ):
        assert legacy_context not in text
