"""`scripts/request.py` is the front door: what it prints and what it refuses.

`build_request.resolve` has its own tests. This file pins the layer above it --
the exit codes, the sections `render` writes, and the branches of `main` that
would otherwise run for the first time in front of an operator at `just request`
(the justfile and .github/scripts/converge-resolve.sh both drive it).

The wrong answers pinned here are the quiet ones:

  EXIT 0 ON A BAD ASK   converge-resolve.sh runs this under `set -e`; an ask the
                        contract rejects has to stop the job, not write an empty
                        plan.
  A MEASUREMENT OF NOTHING  `--measure` on a target with no generated build
                        order, or a build order missing from the tree, has to
                        say so rather than report 0/0 as "done".
  A REWRITE ON A NO-OP  `--adopt` on a request that is not a move must not
                        touch a declaration.

Nothing here reaches the network: the live-index measurement is replaced by a
fake, and `adopt` by a recorder, so the tree is never edited.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import build_request as br  # noqa: E402
import request as cli  # noqa: E402

# Declared contract today: hummingbird has a generated build order, el10 does not.
SERVED = "gnome 51 on hummingbird"
MOVE = "gnome 52 on hummingbird"
HAND_CURATED = "gnome 50 on el10"


def _measurement(**over) -> dict:
    base = {
        "served": 3,
        "wanted": 5,
        "remaining": ["a", "b"],
        "unreachable_indexes": [],
        "stages": [],
    }
    base.update(over)
    return base


class _FakeConverge:
    """Stands in for scripts/plan-converge.py so no index is fetched."""

    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple] = []

    def measure(self, order, urls, cache, **kwargs):
        self.calls.append((order, list(urls), cache, kwargs))
        return self.result


def _use_converge(monkeypatch, result: dict) -> _FakeConverge:
    fake = _FakeConverge(result)
    monkeypatch.setattr(cli, "_converge", lambda: fake)
    return fake


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------


def test_render_names_the_coordinates_and_the_regeneration_command() -> None:
    plan = br.resolve(SERVED)
    text = cli.render(plan, None)
    assert text.startswith("# gnome 51 on hummingbird\n")
    assert f"- roots manifest  : {plan.roots_manifest}" in text
    assert f"- build order     : {plan.build_order}" in text
    assert f"scripts/measure-target-gap.py --target {plan.target}" in text
    assert f"--build-order {plan.build_order}" in text
    assert "This is a move" not in text


def test_render_of_a_target_that_cannot_answer_says_why_and_omits_the_command() -> None:
    plan = br.resolve(HAND_CURATED)
    assert plan.unmeasurable
    text = cli.render(plan, None)
    assert "## This target cannot answer a request yet" in text
    assert plan.unmeasurable in text
    assert "(none — curated by hand)" in text
    assert "measure-target-gap.py" not in text


def test_render_of_a_move_lists_what_moves_and_what_is_left_to_the_operator() -> None:
    plan = br.resolve(MOVE)
    assert plan.is_move
    text = cli.render(plan, None)
    assert f"## This is a move: {plan.declared_track_dir} -> {plan.track_dir}" in text
    assert "Run again with `--adopt` to move them." in text
    for declaration in plan.declarations:
        assert f"  - {declaration.path.relative_to(cli.ROOT)} " in text
    if plan.decisions:
        assert "`--adopt` will NOT rewrite" in text
        for declaration in plan.decisions:
            assert f"  - {declaration.path.relative_to(cli.ROOT)} " in text
    for note in plan.notes:
        assert f"> {note}" in text


def test_render_of_a_measurement_without_stages_falls_back_to_the_raw_count() -> None:
    text = cli.render(br.resolve(SERVED), _measurement())
    assert "- served  : 3/5" in text
    assert "- remaining: 2" in text
    # Existing packages are not a booting desktop; the text must keep saying so.
    assert "whether the desktop BOOTS is tunaOS's Gate" in text
    assert "could not be read" not in text


def test_render_of_a_measurement_with_stages_uses_the_stage_table() -> None:
    stages = [{"name": "contract", "wanted": 4, "remaining": ["gdm"]}]
    text = cli.render(br.resolve(SERVED), _measurement(stages=stages))
    assert "| `contract` | 3/4 | 1 |" in text
    assert "- served  :" not in text
    assert "Open stage: **contract**" in text


def test_render_reports_unreadable_indexes_as_a_floor() -> None:
    down = [{"url": "https://example.invalid/x86_64/", "error": "HTTP 503"}]
    text = cli.render(br.resolve(SERVED), _measurement(unreachable_indexes=down))
    assert "`served` is a floor" in text
    assert "  - https://example.invalid/x86_64/: HTTP 503" in text


# --------------------------------------------------------------------------
# main: refusals
# --------------------------------------------------------------------------


def test_an_ask_the_contract_rejects_exits_2_and_prints_nothing_on_stdout(
    capsys,
) -> None:
    assert cli.main(["a request that is not a request"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error: ")


def test_measure_without_a_generated_build_order_is_refused(
    monkeypatch, capsys
) -> None:
    fake = _use_converge(monkeypatch, _measurement())
    assert cli.main([HAND_CURATED, "--measure"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "--measure needs a generated build order" in err
    assert "el10 declares none" in err
    assert fake.calls == []


def test_measure_with_a_build_order_missing_from_the_tree_is_refused(
    monkeypatch, capsys
) -> None:
    plan = br.resolve(SERVED)
    monkeypatch.setattr(
        br, "resolve", lambda _request: _with_build_order(plan, "no-such-order.yml")
    )
    fake = _use_converge(monkeypatch, _measurement())
    assert cli.main([SERVED, "--measure"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "no-such-order.yml is not in the tree" in err
    assert fake.calls == []


def _with_build_order(plan: br.Plan, build_order: str) -> br.Plan:
    import dataclasses

    return dataclasses.replace(plan, build_order=build_order)


# --------------------------------------------------------------------------
# main: the plain path, --json, --measure
# --------------------------------------------------------------------------


def test_a_plain_request_prints_the_plan_and_exits_0(capsys) -> None:
    assert cli.main([SERVED]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("# gnome 51 on hummingbird\n")
    assert err == ""


def test_json_writes_the_plan_without_a_measurement_key(tmp_path, capsys) -> None:
    target = tmp_path / "plan.json"
    assert cli.main([SERVED, "--json", str(target)]) == 0
    capsys.readouterr()
    payload = json.loads(target.read_text())
    assert target.read_text().endswith("}\n")
    assert (payload["desktop"], payload["release"], payload["target"]) == (
        "gnome", "51", "hummingbird",
    )
    assert "measurement" not in payload


def test_measure_hands_the_contracts_indexes_to_the_engine(
    monkeypatch, tmp_path, capsys
) -> None:
    plan = br.resolve(SERVED)
    fake = _use_converge(monkeypatch, _measurement())
    target = tmp_path / "plan.json"

    assert cli.main([SERVED, "--measure", "--json", str(target)]) == 0

    (order, urls, cache, kwargs), = fake.calls
    assert order == cli.ROOT / plan.build_order
    assert urls == [
        u for arch in plan.architectures for u in plan.served_index.get(arch, [])
    ]
    assert cache == pathlib.Path(".cache/converge")
    assert kwargs == {"roots_manifest": plan.roots_manifest, "desktop": plan.desktop}

    out = capsys.readouterr().out
    assert "- served  : 3/5" in out
    payload = json.loads(target.read_text())
    assert payload["measurement"]["served"] == 3
    assert payload["measurement"]["remaining"] == ["a", "b"]


# --------------------------------------------------------------------------
# main: --adopt
# --------------------------------------------------------------------------


@pytest.fixture
def adopted(monkeypatch) -> list[bool]:
    """Record `adopt` calls instead of rewriting the tree."""
    calls: list[bool] = []

    def fake_adopt(plan, *, apply=False):
        calls.append(apply)
        return {"manifests/example.yaml": 2, "README.md": 1}

    monkeypatch.setattr(br, "adopt", fake_adopt)
    return calls


def test_adopt_on_a_request_that_is_not_a_move_changes_nothing(
    adopted, capsys
) -> None:
    assert cli.main([SERVED, "--adopt"]) == 0
    out, err = capsys.readouterr()
    assert adopted == []
    assert "nothing to adopt" in err
    assert "already declares gnome 51" in err
    assert out.startswith("# gnome 51 on hummingbird\n")


def test_adopt_on_a_move_applies_it_and_says_what_it_left_alone(
    adopted, capsys
) -> None:
    plan = br.resolve(MOVE)
    assert plan.is_move

    assert cli.main([MOVE, "--adopt"]) == 0

    out, err = capsys.readouterr()
    assert adopted == [True]
    # Sorted by path so the report is stable.
    assert err.index("moved 1 reference(s) in README.md") < err.index(
        "moved 2 reference(s) in manifests/example.yaml"
    )
    for decision in plan.decisions:
        assert f"NOT rewritten: {decision.path.relative_to(cli.ROOT)}" in err
    # The build order still describes the old release; the operator is told.
    assert f"still describes {plan.declared_release}" in err
    assert out.startswith("# gnome 52 on hummingbird\n")
