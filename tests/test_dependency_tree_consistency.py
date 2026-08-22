"""Cross-check the dependency trees against the recipes' declared dependencies.

The trees in manifests/dependency-trees/ claim to be the shared source graph,
but nothing connected their edges to what the recipes actually require, which
is how cosmic-comp runtime-required cosmic-icon-theme (and the gate staged it
as a prerequisite artifact) while the cosmic tree's node carried no such edge.
A tree that disagrees with the recipes cannot be used to derive anything.

Two directions:

* Every factory-built runtime dependency a recipe declares must be an edge of
  that package's tree node.  A missing edge is exactly the cosmic-comp bug.
* Every tree edge pointing at a factory-built package must be explained by the
  recipe -- as a runtime dependency, as a build dependency (the trees encode
  build ordering too: cpptrace-devel -> libunwind-devel), or as a reviewed
  functional-coupling edge listed in KNOWN_UNDECLARED_EDGES below.  An
  unexplained edge is either stale or a dependency the recipe forgot.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tideforge  # noqa: E402

TREES = sorted((ROOT / "manifests" / "dependency-trees").glob("*.yaml"))
CATALOG = tideforge.load_dependency_catalog()
PACKAGES = {
    path.parent.name: yaml.safe_load(path.read_text())
    for path in (ROOT / "packages").glob("*/package.yaml")
    if path.parent.name != "_template"
}

# Tree edges to factory-built packages that no recipe dependency declares but
# that survive review as intentional ordering/coupling edges. Each entry is a
# claim someone can audit; a new unexplained edge fails the test instead of
# joining this list silently.
KNOWN_UNDECLARED_EDGES = {
    # The session queue's gates end in greetd-login: greetd must be built and
    # shipped for the stack to be testable, though cosmic-session's own
    # Requires reaches greetd only transitively via cosmic-greeter.
    ("cosmic", "cosmic-session"): {"greetd"},
    # The portal backend is functionally coupled to the compositor and the
    # settings daemon's config surface; neither is a package Requires.
    ("cosmic", "xdg-desktop-portal-cosmic"): {"cosmic-comp", "cosmic-settings"},
    # The KRunner plugin queries the Bazaar D-Bus service at search time; the
    # plugin installs without it, so it is ordering, not Requires.
    ("aurora-kde", "krunner-bazaar"): {"bazaar"},
    # The greeter presents the DankMaterialShell experience; its packaged
    # runtime needs are greetd and quickshell only.
    ("niri-dms", "dms-greeter"): {"dms"},
}


def _provides() -> dict[str, str]:
    """Map every name a recipe answers to (dir name, install_names) to its dir."""
    provides: dict[str, str] = {}
    for name, recipe in PACKAGES.items():
        provides[name] = name
        verify = recipe.get("verify") or {}
        if verify.get("install_name"):
            provides[verify["install_name"]] = name
        for override in (verify.get("targets") or {}).values():
            if override.get("install_name"):
                provides[override["install_name"]] = name
    return provides


PROVIDES = _provides()


def _base(dependency: str) -> str:
    return dependency.split()[0].split(">=")[0].split("<=")[0].split("=")[0].strip()


def _factory_dependencies(recipe: dict, kind: str) -> set[str]:
    block = (recipe.get("dependencies") or {}).get(kind) or {}
    names = list(block.get("common") or [])
    for target_list in (block.get("targets") or {}).values():
        names.extend(target_list or [])
    # Capabilities were invisible here. A capability resolves to a different
    # native name per target and a tree edge names one of them, so a
    # capability-declared dependency read as UNDECLARED -- latent until
    # quickshell stopped hand-listing ninja and started deriving it from
    # build.cmake_generator (#478), at which point a real, still-required
    # edge looked stale. Expanding every target's mapping keeps a capability
    # exactly as visible as a hand-written name.
    capabilities = list(block.get("capabilities") or [])
    if kind == "build":
        capabilities += tideforge.implied_capabilities(recipe)
    for capability in capabilities:
        for target_packages in (CATALOG.get(capability) or {}).values():
            names.extend(target_packages or [])
    return {PROVIDES[_base(name)] for name in names if _base(name) in PROVIDES}


def _tree_cases() -> list[tuple[str, str, dict, dict]]:
    cases = []
    for tree_path in TREES:
        tree = yaml.safe_load(tree_path.read_text())
        for node, spec in (tree.get("nodes") or {}).items():
            if node in PACKAGES:
                cases.append((tree.get("tree", tree_path.stem), node, spec or {}, PACKAGES[node]))
    return cases


CASES = _tree_cases()


def test_trees_actually_reference_recipes() -> None:
    """An empty case list would make the tests below vacuously pass."""
    assert len(CASES) >= 30


@pytest.mark.parametrize("tree,node,spec,recipe", CASES, ids=[f"{t}-{n}" for t, n, _, _ in CASES])
def test_factory_runtime_deps_are_tree_edges(tree: str, node: str, spec: dict, recipe: dict) -> None:
    runtime = _factory_dependencies(recipe, "runtime")
    edges = {PROVIDES[n] for n in spec.get("needs") or [] if n in PROVIDES}
    missing = runtime - edges
    assert not missing, (
        f"{tree}: {node} runtime-requires factory-built {sorted(missing)} "
        "but the tree node has no such edge -- the tree cannot derive the "
        "staging this package needs."
    )


@pytest.mark.parametrize("tree,node,spec,recipe", CASES, ids=[f"{t}-{n}" for t, n, _, _ in CASES])
def test_tree_edges_are_explained_by_the_recipe(tree: str, node: str, spec: dict, recipe: dict) -> None:
    edges = {PROVIDES[n] for n in spec.get("needs") or [] if n in PROVIDES}
    explained = (
        _factory_dependencies(recipe, "runtime")
        | _factory_dependencies(recipe, "build")
        | KNOWN_UNDECLARED_EDGES.get((tree, node), set())
    )
    unexplained = edges - explained
    assert not unexplained, (
        f"{tree}: {node} has edges to factory-built {sorted(unexplained)} that "
        "no recipe dependency declares. Either the recipe under-declares, the "
        "edge is stale, or it is a reviewed coupling that belongs in "
        "KNOWN_UNDECLARED_EDGES with a justification."
    )
