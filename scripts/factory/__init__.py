"""Importable package for shared package-factory services.

CLI entry points remain in ``scripts/`` so their existing command paths stay
stable. Shared code imports through this package instead of loading sibling
files by filesystem path.
"""

__all__ = (
    "consumer_contract",
    "factory_contract",
    "publisher_contract",
    "target_platform",
)
