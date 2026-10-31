"""What we publish for Hummingbird must resolve on Hummingbird, and CI must say so.

scripts/check-hummingbird-installability.py is the static half of the gate
utah-packages runs inside the bootc-os image before it publishes: with only
the target's repository and our published prefix, does every desktop root
resolve?  Measured 2026-09-02 on the live x86_64 indexes, no desktop did --
GNOME had 30 unresolved capabilities, three of them libm.so.6(GLIBC_2.44)
from packages this factory built in a Rawhide root.  The chain had been
reporting those packages as built for weeks.

These tests drive the checker with synthetic indexes so its verdict logic is
pinned without the network, and hold that the scheduled workflow runs it.
"""

from __future__ import annotations

import importlib.util
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_installability", ROOT / "scripts" / "check-hummingbird-installability.py"
)
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)


def index(packages: dict[str, dict]) -> dict:
    """Build a parsed-primary-shaped index from {name: {requires, provides}}."""
    out = {"packages": {}, "provides": {}, "provides_evr": {}, "files": set()}
    for name, spec in packages.items():
        out["packages"][name] = {
            "arch": "x86_64", "evr": spec.get("evr", "1-1"), "srpm": f"{name}-1-1.src.rpm",
            "requires": list(spec.get("requires", [])),
            "requires_versioned": [],
        }
        out["provides"].setdefault(name, set()).add(name)
        for cap in spec.get("provides", []):
            out["provides"].setdefault(cap, set()).add(name)
    return out


CATALOG = {"desktops": {
    "tiny": {"required_packages": ["desktop-shell"], "install_packages": ["desktop-shell", "file-manager"]},
}}


def test_a_closed_closure_is_resolvable():
    target = index({"glibc": {"provides": ["libc.so.6(GLIBC_2.43)(64bit)"]}, "glib2": {}})
    published = index({
        "desktop-shell": {"requires": ["glib2", "libc.so.6(GLIBC_2.43)(64bit)", "file-manager"]},
        "file-manager": {"requires": ["glib2"]},
    })
    report = chk.check(CATALOG, target, published, ["tiny"])
    assert report["tiny"]["resolvable"], report
    assert report["tiny"]["closure_packages"] == 4
    assert report["tiny"]["unresolved"] == {}


def test_a_rawhide_glibc_leak_is_named_and_attributed_to_the_published_side():
    """The GLIBC_2.44 shape: our package requires a symbol the target's
    glibc does not provide.  The report must say which side needs it, because
    a needer in OUR prefix means rebuild in the right root, while a needer in
    the target means the base OS itself is short a package."""
    target = index({"glibc": {"provides": ["libc.so.6(GLIBC_2.43)(64bit)"]}})
    published = index({
        "desktop-shell": {"requires": ["libm.so.6(GLIBC_2.44)(64bit)"]},
        "file-manager": {},
    })
    report = chk.check(CATALOG, target, published, ["tiny"])
    r = report["tiny"]
    assert not r["resolvable"]
    assert list(r["unresolved"]) == ["libm.so.6(GLIBC_2.44)(64bit)"]
    entry = r["unresolved"]["libm.so.6(GLIBC_2.44)(64bit)"]
    assert entry["needed_by"] == ["desktop-shell"]
    assert entry["needer_from"] == ["published"]


def test_a_retained_served_package_is_audited_outside_desktop_closures():
    """#557's pandoc-cli had left every desktop closure but remained served.

    Retained roots must still expose the ABI leak, or dropping a package from
    the generated desktop order makes its broken repository artifact invisible.
    """
    catalog = {
        "retained_packages": ["pandoc-cli"],
        "desktops": {},
    }
    target = index({"glibc": {"provides": ["libm.so.6(GLIBC_2.43)(64bit)"]}})
    published = index({
        "pandoc-cli": {"requires": ["libm.so.6(GLIBC_2.44)(64bit)"]},
    })

    report = chk.check(catalog, target, published, [chk.RETAINED_SET])

    assert report["retained"]["roots"] == 1
    assert report["retained"]["unresolved"] == {
        "libm.so.6(GLIBC_2.44)(64bit)": {
            "needed_by": ["pandoc-cli"],
            "needer_from": ["published"],
            "class": "absent",
        },
    }


def test_a_root_in_neither_repository_is_reported_not_swallowed():
    target = index({})
    published = index({"desktop-shell": {}})
    report = chk.check(CATALOG, target, published, ["tiny"])
    assert report["tiny"]["roots_absent"] == ["file-manager"]
    assert not report["tiny"]["resolvable"]


def test_the_published_prefix_wins_a_name_collision_but_the_target_still_provides():
    """Both repos ship `glib2`; the walk must not lose the target's other
    provides just because our copy of glib2 is the one indexed by name."""
    target = index({"glib2": {"evr": "2.89.3-1.hum1", "provides": ["libglib-2.0.so.0()(64bit)"]},
                    "only-in-target": {}})
    published = index({"glib2": {"evr": "2.89.3-1.bfin1"},
                       "desktop-shell": {"requires": ["libglib-2.0.so.0()(64bit)", "only-in-target"]},
                       "file-manager": {}})
    merged = chk.merge_indexes(published, target)
    assert merged["packages"]["glib2"]["evr"] == "2.89.3-1.bfin1"
    assert "libglib-2.0.so.0()(64bit)" in merged["provides"]
    report = chk.check(CATALOG, target, published, ["tiny"])
    assert report["tiny"]["resolvable"], report["tiny"]["unresolved"]


def test_the_report_renders_blockers_first():
    target = index({})
    published = index({"desktop-shell": {"requires": ["libmissing.so.1()(64bit)"]}, "file-manager": {}})
    text = chk.render(chk.check(CATALOG, target, published, ["tiny"]))
    assert "| `tiny` |" in text and "❌" in text
    assert ("`libmissing.so.1()(64bit)` needed by `desktop-shell` "
            "(published, absent)") in text


def test_the_checker_runs_on_a_schedule_and_on_the_inputs_that_move_it():
    wf = ROOT / ".github" / "workflows" / "hummingbird-installability.yml"
    doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))
    assert "schedule" in on, "a gate that only runs by hand is not a gate"
    assert "workflow_dispatch" in on
    body = wf.read_text(encoding="utf-8")
    assert "scripts/check-hummingbird-installability.py" in body
    assert "GITHUB_STEP_SUMMARY" in body, "the report is the product; it must reach the summary"
    assert doc["permissions"] == {"contents": "read"}, "read-only; it publishes nothing"
