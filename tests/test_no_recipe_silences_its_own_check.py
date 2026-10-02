"""No recipe may define tests_nonfatal to silence its own %check.

Fedora specs such as pipewire share a %check shaped like this::

    %meson_test || TESTS_ERROR=$?
    if [ "${TESTS_ERROR}" != "" ]; then
    echo "test failed"
    %{!?tests_nonfatal:exit $TESTS_ERROR}
    fi

One ``%global tests_nonfatal 1`` above that block turns a failing test suite
into a successful build. The build log still prints "test failed", but no
other part of the factory sees it. projectbluefin/utah-packages added this
gate after that line was almost used to get a hanging pipewire test green,
which would have published a broken audio stack.

This test catches only that one route. A recipe can still remove the exit
guard or append ``|| :`` to the test command. Do not do those either: fix
the test or the build environment, do not hide the failure.

No recipe in this repository defines the macro today, so the allowlist is
empty. An entry records an exact count of definitions that a Fedora import
brought in, not permission to add more.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFINITION = re.compile(r"^\s*%(?:global|define)\s+tests_nonfatal\b", re.MULTILINE)

# Spec path relative to the repository root -> number of inherited definitions.
INHERITED: dict[str, int] = {}


def _definitions() -> Counter[str]:
    found: Counter[str] = Counter()
    for spec in sorted((ROOT / "src").rglob("*.spec")):
        count = len(DEFINITION.findall(spec.read_text(encoding="utf-8", errors="replace")))
        if count:
            found[spec.relative_to(ROOT).as_posix()] = count
    return found


def test_the_pattern_matches_a_definition() -> None:
    assert DEFINITION.search("%global tests_nonfatal 1")
    assert DEFINITION.search("  %define tests_nonfatal 1")
    assert not DEFINITION.search("%{!?tests_nonfatal:exit $TESTS_ERROR}")
    assert not DEFINITION.search("%global tests_nonfatal_extra 1")


def test_no_recipe_defines_tests_nonfatal() -> None:
    found = _definitions()
    added = {spec: count for spec, count in found.items() if count > INHERITED.get(spec, 0)}
    assert added == {}, (
        "These recipes define tests_nonfatal, which makes a failing %check "
        f"pass. Fix the failing test instead: {added}"
    )


def test_every_allowlist_entry_is_still_needed() -> None:
    found = _definitions()
    stale = {spec: count for spec, count in INHERITED.items() if found.get(spec, 0) < count}
    assert stale == {}, f"Lower or remove these INHERITED entries: {stale}"
