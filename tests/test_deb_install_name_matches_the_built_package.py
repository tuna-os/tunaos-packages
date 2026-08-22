"""A recipe whose deb output is renamed must verify against that name.

cpptrace-devel's deb cells had never passed. They BUILD fine and then die in
verification (run 32550564218, all four):

    dpkg-scanpackages: warning:   libcpptrace-dev     <- what was built
    E: Unable to locate package cpptrace-devel        <- what verify asked for

`verify_metadata` defaults install_name to the recipe name and lets a target
override it -- its own docstring cites xfconf, which is `xfconf` on el10 and
`libxfconf-0-4` on deb. cpptrace-devel declared the renamed deb output in
outputs.deb.packages but never the matching override, so the two halves of
the same fact disagreed.

This is a trap rather than a one-off: nothing ties outputs.deb.packages[].name
to verify.targets.<t>.install_name, so any future recipe that renames a deb
output inherits the same silent failure. The general fix -- deriving
install_name from a sole deb output -- is deliberately NOT done here, because
it changes a shared resolver for every recipe; it is called out so the next
one does not have to rediscover it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tideforge  # noqa: E402


def recipes_with_renamed_deb_outputs():
    """Every recipe whose deb output name differs from the recipe name."""
    for path in sorted((ROOT / "packages").glob("*/package.yaml")):
        recipe = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        packages = ((recipe.get("outputs") or {}).get("deb") or {}).get("packages") or []
        names = [p.get("name") for p in packages if isinstance(p, dict)]
        if len(names) == 1 and names[0] and names[0] != recipe.get("name"):
            yield path, recipe, names[0]


def test_cpptrace_verifies_against_its_deb_output_name():
    recipe = yaml.safe_load(
        (ROOT / "packages" / "cpptrace-devel" / "package.yaml").read_text(encoding="utf-8")
    )
    for target in ("ubuntu", "debian"):
        assert tideforge.verify_metadata(recipe, target)["install_name"] == "libcpptrace-dev", target


def test_the_rpm_targets_keep_the_recipe_name():
    """The override must be scoped to deb; applying it everywhere would break
    el10, which really does ship cpptrace-devel."""
    recipe = yaml.safe_load(
        (ROOT / "packages" / "cpptrace-devel" / "package.yaml").read_text(encoding="utf-8")
    )
    for target in ("el10", "arch", "opensuse-tumbleweed"):
        assert tideforge.verify_metadata(recipe, target)["install_name"] == "cpptrace-devel", target


def test_every_renamed_deb_output_is_verified_under_that_name():
    """The sweep. A recipe that renames its deb output and forgets the
    override builds clean and fails verification -- the exact failure above,
    and one no single-recipe test would catch for the next one."""
    offenders = []
    for path, recipe, deb_name in recipes_with_renamed_deb_outputs():
        for target in recipe.get("targets") or []:
            fmt_is_deb = target in {"ubuntu", "debian"}
            if not fmt_is_deb:
                continue
            resolved = tideforge.verify_metadata(recipe, target)["install_name"]
            if resolved != deb_name:
                offenders.append((path.parent.name, target, resolved, deb_name))
    assert not offenders, offenders
