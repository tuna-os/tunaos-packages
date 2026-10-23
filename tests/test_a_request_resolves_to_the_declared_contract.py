"""A request is answered by the contract, not by the resolver's own opinions.

`scripts/build_request.py` turns "gnome 51 on hummingbird" into the coordinates
a wave needs. Everything it returns must come out of
manifests/package-factory.yaml, the roots manifest that contract names, and
manifests/package-builds.yaml -- because the moment any of it is hard-coded
here, adding a target becomes a code change and the resolver becomes a second,
drifting definition of what a target is. That is the exact defect
scripts/factory/factory_contract.py exists to prevent on the other side of the same
manifests.

The wrong answers pinned here are the quiet ones:

  WRONG CELLS   el10 carries gnome50, gnome51, xfce and fprintd on ONE target.
                An unfiltered cell list makes "gnome 51 on el10" dispatch
                xfce and fprintd -- work nobody asked for, on runners the
                request was meant to spend elsewhere.
  INVENTED MOVE a target with no roots manifest declares no release, so there
                is nothing to move FROM. Reporting a move there would offer
                `--adopt` on six declarations that do not exist.
  SILENT GAP    el10 has no gap_measurement, so its build order is curated by
                hand and cannot be regenerated. Answering the request as
                though it could is how a request quietly measures nothing.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import build_request as br  # noqa: E402


@pytest.mark.parametrize(
    "spelling",
    [
        "gnome 51 on hummingbird",
        "gnome:51@hummingbird",
        "gnome 51 hummingbird",
        "GNOME 51 on Hummingbird",
    ],
)
def test_every_spelling_reaches_the_same_coordinates(spelling: str) -> None:
    plan = br.resolve(spelling)
    assert (plan.desktop, plan.release, plan.target) == (
        "gnome", "51", "hummingbird",
    )


def test_a_release_is_optional_and_comes_from_the_manifest() -> None:
    """Asking without a release means "whatever this target tracks"."""
    catalog = yaml.safe_load(
        (REPO / "manifests" / "hummingbird-desktops.yaml").read_text()
    )
    declared = str(catalog["desktops"]["gnome"]["release"])
    assert br.resolve("gnome on hummingbird").release == declared
    assert not br.resolve("gnome on hummingbird").is_move


def test_an_unknown_target_names_the_ones_that_exist() -> None:
    with pytest.raises(br.RequestError) as error:
        br.resolve("gnome 51 on nosuchtarget")
    # Naming the alternatives is the difference between an error a person can
    # act on and one they have to go read a manifest to understand.
    assert "hummingbird" in str(error.value)


def test_an_unknown_desktop_names_the_ones_that_exist() -> None:
    with pytest.raises(br.RequestError) as error:
        br.resolve("plasma on hummingbird")
    assert "gnome" in str(error.value) and "xfce" in str(error.value)


def test_a_request_for_one_release_does_not_dispatch_the_other() -> None:
    """el10 carries gnome50 and gnome51 side by side on one target."""
    fifty = br.resolve("gnome 50 on el10").cells
    fifty_one = br.resolve("gnome 51 on el10").cells
    assert fifty, "gnome 50 on el10 must resolve to cells"
    assert fifty_one, "gnome 51 on el10 must resolve to cells"
    assert not set(fifty) & set(fifty_one), (
        f"a gnome 50 request selected gnome 51 cells: {fifty} vs {fifty_one}"
    )
    assert all("gnome50" in cell for cell in fifty)
    assert all("gnome51" in cell for cell in fifty_one)


def test_a_target_with_one_family_keeps_every_cell() -> None:
    """hummingbird's single family covers every desktop it declares.

    Filtering by desktop NAME there matches nothing (the family is
    `hummingbird-desktops`), and a filter that falls through to an empty list
    would resolve a valid request to zero cells -- a request that silently
    builds nothing.
    """
    plan = br.resolve("gnome 51 on hummingbird")
    assert set(plan.cells) == {"hummingbird-x86_64", "hummingbird-aarch64"}
    assert set(br.resolve("niri on hummingbird").cells) == set(plan.cells)


def test_a_target_with_no_roots_manifest_reports_it_rather_than_measuring() -> None:
    plan = br.resolve("gnome 51 on el10")
    assert plan.unmeasurable, (
        "el10 declares no gap_measurement; a request must say so rather than "
        "answer as though a build order could be regenerated"
    )
    assert "gap_measurement" in plan.unmeasurable
    assert not plan.is_move, (
        "a target that declares no release cannot be 'moved' off one"
    )


def test_every_declared_index_is_carried_per_arch() -> None:
    """Convergence is measured against the served index, per architecture.

    An arch resolving to an EMPTY index list is the shape #467/#471 record:
    published_index.py resolves per-arch, and an arch that silently got none
    made every factory-built dependency read as NOT AVAILABLE. So the plan
    carries a list per declared arch, empty or not, and never drops the key.
    """
    plan = br.resolve("gnome 51 on hummingbird")
    assert set(plan.served_index) == set(plan.architectures)
    for arch, urls in plan.served_index.items():
        assert isinstance(urls, list), arch
        assert all(url.startswith("https://") for url in urls), arch


def test_a_disabled_cell_is_never_dispatched() -> None:
    """gnome49-el10-x86_64 is `enabled: false` -- legacy, deliberately dark."""
    builds = yaml.safe_load(
        (REPO / "manifests" / "package-builds.yaml").read_text()
    )
    disabled = {
        cell["id"] for cell in builds["native_builds"]
        if cell.get("enabled") is False
    }
    assert disabled, "this test is vacuous with no disabled cell in the tree"
    for request in ("gnome 49 on el10", "gnome 50 on el10", "gnome 51 on el10"):
        assert not set(br.resolve(request).cells) & disabled, request


def test_a_request_that_is_not_a_request_says_how_to_write_one() -> None:
    with pytest.raises(br.RequestError) as error:
        br.resolve("!!!")
    assert "gnome 51 on hummingbird" in str(error.value)
