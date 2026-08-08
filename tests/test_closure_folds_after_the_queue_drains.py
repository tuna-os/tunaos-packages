"""The BuildRequires fold must run when the runtime queue drains.

It used to hang off the end of the runtime loop body, after an early
`continue`:

    while queue:
        name = queue.pop()
        if name in seen:
            continue                     # <-- jumps past the fold below
        seen.add(name)
        ...
        if not queue and source_index is not None:
            ...fold...

When the queue drained on an already-seen name -- a duplicate, which is
common, since every shared library is queued by many packages -- control went
straight back to `while queue:`, found it empty, and left. The newest sources
were never folded.

Whether a desktop got its build dependencies at all came down to whether the
last pop happened to be a duplicate, which is why it looked like a
per-desktop problem: measured against Fedora Rawhide, cosmic reached `vala`
and ordered `dconf` after it at tier 11, while gnome never reached it and
sorted `dconf` into tier 0 with no build dependencies -- where it failed
against Rawhide's Python 3.15 (#287).

The fix is structural: walk and fold are separate passes alternating to a
fixpoint, so no early exit in one can skip the other.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "gap", REPO / "scripts" / "measure-hummingbird-gap.py"
)
GAP = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GAP)


def reference(packages):
    provides = {}
    for name in packages:
        provides.setdefault(name, set()).add(name)
    return {"packages": packages, "provides": provides}


def pkg(srpm, requires=()):
    return {"srpm": f"{srpm}-1-1.fc45.src.rpm", "requires": list(requires)}


def test_the_last_fold_is_not_skipped_by_a_duplicate_pop():
    """The regression, reduced.

    `app` runtime-requires `libа` twice over, so the queue drains on a
    duplicate. Its BuildRequires `tool` must still be reached.
    """
    packages = {
        "app": pkg("app", ["lib"]),
        "lib": pkg("lib", []),
        "tool": pkg("tool", []),
    }
    # Queue `lib` from two places so the final pop is an already-seen name.
    packages["app"]["requires"] = ["lib", "lib"]
    source_index = {"app": ["tool"]}
    seen, absent, unresolved = GAP.closure(
        ["app"], reference(packages), set(), source_index
    )
    assert "tool" in seen, (
        "the BuildRequires fold was skipped because the queue drained on a "
        "duplicate -- this is the bug that hid ~90% of the build closure"
    )


def test_fold_reaches_transitively():
    """A build tool's own build tool must be reached too."""
    packages = {
        "app": pkg("app"),
        "tool": pkg("tool"),
        "tool-of-tool": pkg("tool-of-tool"),
    }
    source_index = {"app": ["tool"], "tool": ["tool-of-tool"]}
    seen, _, _ = GAP.closure(["app"], reference(packages), set(), source_index)
    assert {"tool", "tool-of-tool"} <= seen


def test_what_the_target_ships_still_stops_the_walk():
    packages = {"app": pkg("app"), "tool": pkg("tool")}
    seen, _, _ = GAP.closure(
        ["app"], reference(packages), {"tool"}, {"app": ["tool"]}
    )
    assert "tool" not in seen, "a package the target already ships must not be built"


def test_without_a_source_index_the_behaviour_is_runtime_only():
    """Pinned so a caller with no source reference is unaffected."""
    packages = {"app": pkg("app", ["lib"]), "lib": pkg("lib"), "tool": pkg("tool")}
    seen, _, _ = GAP.closure(["app"], reference(packages), set(), None)
    assert seen == {"app", "lib"}


def test_it_terminates_on_a_buildrequires_cycle():
    """glib2 BuildRequires gobject-introspection BuildRequires glib2."""
    packages = {"a": pkg("a"), "b": pkg("b")}
    seen, _, _ = GAP.closure(
        ["a"], reference(packages), set(), {"a": ["b"], "b": ["a"]}
    )
    assert seen == {"a", "b"}


def test_an_unresolvable_buildrequires_is_reported_not_dropped():
    packages = {"app": pkg("app")}
    seen, _, unresolved = GAP.closure(
        ["app"], reference(packages), set(), {"app": ["nothing-provides-this"]}
    )
    assert "nothing-provides-this" in unresolved, (
        "a BuildRequires nothing provides must be reported; silence there is "
        "how 413 missing capabilities went unnoticed"
    )
