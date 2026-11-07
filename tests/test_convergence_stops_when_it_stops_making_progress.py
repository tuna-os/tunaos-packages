"""A loop that cannot tell "still working" from "stuck" burns runners forever
— and one that measures the wrong thing burns them on the wrong packages.

scripts/plan-converge.py decides whether a convergence dispatches another
wave. What it decides on is NOT a count over the build order. Measured against
the live hummingbird index on 2026-08-28, the same repo on the same day reads:

    build order          580/673   (86%)
    desktop contract       3/10

A loop optimising the first number spends waves on vdirsyncer and hplip while
gdm and gnome-shell are absent — every one of them moves 580/673 upward and
moves the stack not at all. So the objective is scripts/stack_readiness.py's
ordered stages, and only the FIRST OPEN one decides anything.

The ways that goes wrong:

  WRONG NUMBER    judging on the whole order, or on a later stage moving while
                  the open one stands still. Both read as progress; neither is
                  progress toward a desktop that comes up.
  NEVER STOPS     the open stage not moving must end the loop.
  STOPS TOO SOON  a first wave has no previous count; reading a missing one as
                  zero ends the loop before it starts.
  FALSE READY     `packages-ready` ends the loop and green-lights an image
                  build, so it must never be reached from an index that could
                  not be read.
  OVERCLAIM       `packages-ready` must not be spelled `done`. Nothing here
                  proves a desktop boots; tunaOS's green-criteria Gate does.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "plan-converge.py"
sys.path.insert(0, str(REPO / "scripts"))

_spec = importlib.util.spec_from_file_location("plan_converge", SCRIPT)
converge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(converge)

import stack_readiness  # noqa: E402


@pytest.mark.parametrize(
    "open_stage,open_remaining,prev_stage,prev_remaining,wave,expected",
    [
        ("", 0, "contract", 7, 2, converge.PACKAGES_READY),
        ("", 0, None, None, 1, converge.PACKAGES_READY),
        ("contract", 7, None, None, 1, converge.CONTINUE),
        ("contract", 4, "contract", 7, 2, converge.CONTINUE),
        ("contract", 7, "contract", 7, 2, converge.BLOCKED),
        ("contract", 9, "contract", 7, 2, converge.BLOCKED),
        # The open stage moved on: stages are ordered, so that is progress by
        # construction — and it is the only signal meaning the STACK advanced
        # rather than merely the repo.
        ("session", 18, "contract", 3, 2, converge.CONTINUE),
    ],
)
def test_the_rules(open_stage, open_remaining, prev_stage, prev_remaining,
                   wave, expected) -> None:
    assert converge.decide(
        open_stage, open_remaining, prev_stage, prev_remaining, wave, 8
    )[0] == expected


def test_a_later_stage_moving_is_not_progress() -> None:
    """The whole point: the tail advancing while the contract stands still."""
    verdict, why = converge.decide("contract", 7, "contract", 7, 2, 8)
    assert verdict == converge.BLOCKED
    assert "packaging change" in why


def test_a_first_wave_is_never_blocked() -> None:
    verdict, why = converge.decide("contract", 7, None, None, 1, 3)
    assert verdict == converge.CONTINUE
    assert "first wave" in why


def test_the_budget_ends_it_even_while_it_is_still_moving() -> None:
    verdict, why = converge.decide("order", 5, "order", 400, 3, 3)
    assert verdict == converge.BUDGET
    assert "budget" in why


def test_the_budget_does_not_override_readiness() -> None:
    assert converge.decide("", 0, "session", 5, 3, 3)[0] == (
        converge.PACKAGES_READY
    )


def test_the_terminal_verdict_does_not_claim_the_desktop_boots() -> None:
    """Only tunaOS's Gate can say `done`; this side says the packages exist."""
    assert converge.PACKAGES_READY == "packages-ready"
    assert not hasattr(converge, "DONE"), (
        "a verdict called `done` on this side of the pipeline claims a "
        "booting desktop that nothing here measured"
    )
    _, why = converge.decide("", 0, None, None, 1, 3)
    assert "BOOTS" in why and "gate" in why.lower()


def test_the_build_order_is_read_the_way_the_index_answers() -> None:
    """Names, not paths: the index has never heard of src/gnome-51.

    Utah supplies GNOME only. Shared dependencies such as gtk4 can still
    appear in the order for other desktops that cannot consume Utah.
    gjs and nautilus use local specs under src/gnome-51."""
    names = converge.wanted_names(
        REPO / "build-order-hummingbird-desktops.yml"
    )
    assert names
    assert not any("/" in name for name in names)
    assert "gjs" in names and "nautilus" in names
    report = json.loads((REPO / "docs/hummingbird-desktop-gap.json").read_text())
    gnome_need = report["desktops"]["gnome"]["source_packages_to_build"]
    assert "gtk4" not in gnome_need and "mutter" not in gnome_need, (
        "GNOME's measurement must count its consumed Utah packages as available"
    )
    assert len(names) == len(set(names))


def test_a_copr_only_entry_is_still_wanted(tmp_path: pathlib.Path) -> None:
    """Some tiers carry `copr_name:` with no path; dropping them undercounts."""
    order = tmp_path / "order.yml"
    order.write_text(
        yaml.safe_dump({"tiers": [{"name": "t0", "packages": [
            {"path": "src/deps/meson"}, {"copr_name": "vala"},
            {"name": "pango"}, "src/deps/harfbuzz",
        ]}]})
    )
    assert converge.wanted_names(order) == [
        "meson", "vala", "pango", "harfbuzz",
    ]


def test_the_stages_partition_rather_than_overlap() -> None:
    """An overlapping session stage would report the same missing gdm twice."""
    required, installed = stack_readiness.desktop_sets(
        "manifests/hummingbird-desktops.yaml", "gnome"
    )
    assert not set(required) & set(installed)
    order = converge.wanted_names(
        REPO / "build-order-hummingbird-desktops.yml"
    )
    staged = stack_readiness.stages(order, required, installed, set())
    names = [n for stage in staged for n in stage.wanted]
    assert len(names) == len(set(names)), "a name landed in two stages"


def test_a_contract_package_the_build_order_omits_is_still_counted(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The most important missing package is one nothing plans to build.

    Scoring only the order's own names would report a clean residue for a
    desktop whose display manager was never in the plan.
    """
    order = tmp_path / "order.yml"
    order.write_text(
        yaml.safe_dump({"tiers": [{"name": "t0", "packages": [{"name": "gtk4"}]}]})
    )
    roots = tmp_path / "roots.yaml"
    roots.write_text(yaml.safe_dump({
        "desktops": {"gnome": {"required_packages": ["gdm", "gtk4"],
                               "install_packages": []}}
    }))
    monkeypatch.setattr(stack_readiness, "ROOT", tmp_path)
    status = converge._load_status()
    monkeypatch.setattr(
        status, "index_names", lambda url, cache: ({"gtk4"}, {"baseurl": url})
    )
    measurement = converge.measure(
        order, ["https://x/"], tmp_path,
        roots_manifest="roots.yaml", desktop="gnome",
    )
    assert measurement["open_stage"] == "contract"
    assert "gdm" in measurement["remaining"], (
        "gdm is required by the desktop and absent from the build order; a "
        "residue that omits it reports a plan that can never boot as complete"
    )


def test_no_roots_manifest_degrades_to_one_flat_stage(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A target with no gap_measurement has no contract to stage by."""
    order = tmp_path / "order.yml"
    order.write_text(
        yaml.safe_dump({"tiers": [{"name": "t0", "packages": [
            {"name": "a"}, {"name": "b"}]}]})
    )
    status = converge._load_status()
    monkeypatch.setattr(
        status, "index_names", lambda url, cache: ({"a"}, {"baseurl": url})
    )
    measurement = converge.measure(order, ["https://x/"], tmp_path)
    assert measurement["open_stage"] == "order"
    assert measurement["remaining"] == ["b"]


def test_an_unreadable_index_can_never_produce_packages_ready(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The dangerous shape: one prefix answers, another 404s.

    A target may publish through several prefixes (#467). If the readable one
    happens to carry every name, every stage reads closed and the loop would
    green-light an image build — on evidence it knows is incomplete. An
    unreachable index understates nothing here only by luck, and
    `packages-ready` is the verdict where that stops being tolerable.
    """
    order = tmp_path / "order.yml"
    order.write_text(
        yaml.safe_dump({"tiers": [{"name": "t0", "packages": [{"name": "a"}]}]})
    )
    status = converge._load_status()

    def answer(url, cache):
        if url == "https://dead/":
            raise OSError("404")
        return {"a"}, {"baseurl": url}

    monkeypatch.setattr(status, "index_names", answer)
    report = tmp_path / "r.json"
    converge.main([
        "--build-order", str(order),
        "--served-index", "https://live/", "--served-index", "https://dead/",
        "--report-json", str(report), "--cache", str(tmp_path / "c"),
    ])
    data = json.loads(report.read_text())
    assert data["open_stage"] == "", "every name was served by the live index"
    assert data["unreachable_indexes"]
    assert data["verdict"] == converge.BLOCKED, (
        "packages-ready green-lights an image build; it must not be reached "
        "from an index that could not be read"
    )
    assert "https://dead/" in data["why"]


def test_several_indexes_union(tmp_path: pathlib.Path, monkeypatch) -> None:
    """A target may publish through more than one prefix (#467)."""
    order = tmp_path / "order.yml"
    order.write_text(
        yaml.safe_dump({"tiers": [{"name": "t0", "packages": [
            {"name": "a"}, {"name": "b"}]}]})
    )
    status = converge._load_status()
    answers = {"https://one/": {"a"}, "https://two/": {"b"}}
    monkeypatch.setattr(
        status, "index_names",
        lambda url, cache: (answers[url], {"baseurl": url}),
    )
    measurement = converge.measure(
        order, ["https://one/", "https://two/"], tmp_path
    )
    assert measurement["remaining"] == []
    assert measurement["open_stage"] == ""


def test_the_cli_refuses_to_measure_against_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """Convergence is measured against the served index, never a run result."""
    order = tmp_path / "order.yml"
    order.write_text(yaml.safe_dump({"tiers": []}))
    with pytest.raises(SystemExit) as exit_:
        converge.main(["--build-order", str(order)])
    assert "--served-index is required" in str(exit_.value)


def test_the_report_records_what_it_measured_against(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    order = tmp_path / "order.yml"
    order.write_text(
        yaml.safe_dump({"tiers": [{"name": "t0", "packages": [{"name": "a"}]}]})
    )
    status = converge._load_status()
    monkeypatch.setattr(
        status, "index_names",
        lambda url, cache: ({"a"}, {"baseurl": url, "revision": "1786016019"}),
    )
    report = tmp_path / "r.json"
    converge.main([
        "--build-order", str(order), "--served-index", "https://x/",
        "--report-json", str(report), "--cache", str(tmp_path / "c"),
    ])
    data = json.loads(report.read_text())
    assert data["verdict"] == converge.PACKAGES_READY
    assert data["index_provenance"][0]["revision"] == "1786016019", (
        "a verdict with no record of which index revision produced it cannot "
        "be re-checked later"
    )
