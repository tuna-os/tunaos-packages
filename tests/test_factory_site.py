"""The published page must not be able to overstate the factory.

A status page people read without a checkout is exactly where a comfortable
lie survives longest, so the properties pinned here are the ones that would
make it flatter than the measurement:

  * a declared target that cannot be measured must still appear, with the
    reason -- otherwise the coverage section reads as the whole picture;
  * a REGRESSED name must reach the page, because that is the repo-wipe shape
    (#519) and the one thing a green-looking dashboard must never bury;
  * a stale measurement must announce itself rather than presenting old
    numbers as current (the #448 shape: a refresh that stopped landing);
  * state is never colour alone -- every chip carries an icon and a word.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "render_factory_site", ROOT / "scripts" / "render-factory-site.py")
site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site)

NOW = "2026-08-25T06:00:00+00:00"

STATUS = {
    "measured_at": "2026-08-25T05:00:00+00:00",
    "targets": {
        "el10": {
            "x86_64": {
                "built": ["a", "b", "c"],
                "needed": ["d"],
                "catalog_entries": 4,
                "indexes": [{"baseurl": "https://repo.example.test/el10/",
                             "package_names": 12}],
            },
        },
    },
    "unmeasured_targets": {
        "arch": {"catalog_entries": 22, "format": "pkg.tar.zst",
                 "reason": "no published_index declared in "
                           "package-factory.yaml"},
    },
    "trend": {"since": "2026-08-24T05:00:00+00:00", "staleness_days": 1,
              "rows": {"el10/x86_64": {"built_delta": 2, "needed_delta": -2,
                                       "new": False, "newly_built": ["b", "c"],
                                       "regressed": [], "stalled": None}}},
    "history": [],
    "desktop_coverage": {},
}

CONTRACT = {
    "targets": {
        "el10": {"status": "supported", "format": "rpm", "buildroot": "epel-10",
                 "architectures": ["x86_64"], "repository": "rpm-md",
                 "published_index": {"x86_64": "https://repo.example.test/el10/"}},
        "arch": {"status": "scaffold", "format": "pkg.tar.zst",
                 "architectures": ["x86_64"], "repository": "pacman"},
    },
}

CELLS = {"native_builds": [
    {"id": "gnome51-el10-x86_64", "family": "gnome51", "track": "next",
     "target": "el10", "architecture": "x86_64", "enabled": True},
    {"id": "gnome49-el10-x86_64", "family": "gnome49", "track": "legacy",
     "target": "el10", "architecture": "x86_64", "enabled": False},
]}


def render(status=None, contract=None, cells=None, now=NOW) -> str:
    return site.render(status or STATUS, contract or CONTRACT,
                       cells or CELLS, site.parse_iso(now))


def text_of(page: str) -> str:
    """The page as a reader sees it: no markup, no styles."""
    body = re.sub(r"<style.*?</style>", " ", page, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


# --- it cannot quietly shrink the problem ----------------------------------


def test_an_unmeasurable_target_is_still_on_the_page_with_its_reason():
    """Otherwise 'coverage' silently means 'the targets that were easy'."""
    page = text_of(render())
    assert "arch" in page
    assert "no published_index declared" in page


def test_every_declared_target_appears_in_the_configuration_section():
    page = text_of(render())
    for name in CONTRACT["targets"]:
        assert name in page, f"{name} is in the contract but not on the page"


def test_a_regressed_name_reaches_the_page():
    """#519: names that were served and no longer are. A dashboard that
    renders that as a smaller bar and no words is worse than no dashboard."""
    status = {**STATUS, "trend": {**STATUS["trend"], "rows": {
        "el10/x86_64": {"built_delta": -1, "needed_delta": 1, "new": False,
                        "newly_built": [], "regressed": ["harfbuzz"],
                        "stalled": None}}}}
    page = text_of(render(status))
    assert "REGRESSED" in page
    assert "1 name no longer served" in page


def test_a_stall_is_reported_with_its_age():
    status = {**STATUS, "trend": {**STATUS["trend"], "rows": {
        "el10/x86_64": {"built_delta": 0, "needed_delta": 0, "new": False,
                        "newly_built": [], "regressed": [],
                        "stalled": {"days": 5, "at_least": True}}}}}
    page = text_of(render(status))
    assert "flat ≥ 5 days" in page


# --- it cannot present old numbers as current ------------------------------


def test_a_fresh_measurement_carries_no_alarm():
    page = text_of(render())
    assert "Stale measurement" not in page


def test_a_stale_measurement_says_so_before_the_numbers_are_believed():
    page = text_of(render(now="2026-09-01T06:00:00+00:00"))
    assert "Stale measurement" in page
    assert "7 days old" in page
    assert "history rather than" in page


def test_the_staleness_threshold_matches_the_measuring_tool():
    """The page and factory-status.py must not disagree about 'stale'."""
    source = (ROOT / "scripts" / "factory-status.py").read_text(
        encoding="utf-8")
    assert f"> {site.STALE_AFTER_DAYS}" in source or \
           f"{site.STALE_AFTER_DAYS} days" in source, (
        "scripts/factory-status.py no longer warns at "
        f"{site.STALE_AFTER_DAYS} days; the page would disagree with it")


# --- accessibility ---------------------------------------------------------


def test_state_is_never_colour_alone():
    """Every chip pairs its colour with an icon and a word."""
    page = render()
    for match in re.finditer(r'<span class="pill pill-(\w+)">(.*?)</span>\s*'
                             r'(?=<)', page):
        chip = match.group(0)
        assert 'class="ico"' in chip, f"chip without an icon: {chip}"
    assert page.count('class="ico"') >= 1


def test_a_table_view_of_the_coverage_exists():
    """The numbers must be readable without reading the bars."""
    page = render()
    assert "Table view" in page
    assert "<table>" in page


def test_the_bar_carries_an_accessible_label():
    page = render()
    assert 'role="img"' in page
    assert 'aria-label="3 of 4 built"' in page


# --- output shape ----------------------------------------------------------


def test_the_page_is_self_contained():
    """No CDN, no external stylesheet, no external script: Pages serves it
    as-is and it opens from a file:// URL.

    This used to forbid <script> outright. The browser pages need a filter
    box, so the rule is now the one that was actually load-bearing: nothing
    is FETCHED. An inline script keeps the page a single file that opens from
    file://; a src= would not. The companion property -- that no content
    comes from script -- is pinned separately by
    test_the_package_rows_are_in_the_html_not_generated_by_script."""
    page = render()
    assert "<script src" not in page.lower()
    assert "http://" not in page.replace("http://www.w3.org", "")
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', page)
    for url in external:
        assert url.startswith(("https://repo.", "https://github.com")), (
            f"page pulls an external asset: {url}")


def test_rendering_is_deterministic():
    assert render() == render()


def test_dark_mode_is_selected_not_inherited():
    page = render()
    assert "prefers-color-scheme:dark" in page
    assert '[data-theme="dark"]' in page
    assert "--surface-1:#1a1a19" in page


def test_the_renderer_writes_nojekyll(tmp_path: pathlib.Path):
    """Pages runs Jekyll unless told not to, and Jekyll drops _-prefixed
    files without saying so."""
    import json

    import yaml
    status = tmp_path / "s.json"
    status.write_text(json.dumps(STATUS), encoding="utf-8")
    contract = tmp_path / "c.yaml"
    contract.write_text(yaml.safe_dump(CONTRACT), encoding="utf-8")
    cells = tmp_path / "b.yaml"
    cells.write_text(yaml.safe_dump(CELLS), encoding="utf-8")
    out = tmp_path / "site"
    assert site.main(["--out", str(out), "--status", str(status),
                      "--contract", str(contract), "--cells", str(cells),
                      "--now", NOW]) == 0
    assert (out / "index.html").is_file()
    assert (out / ".nojekyll").is_file()


def test_it_renders_the_repositorys_real_data():
    """The fixtures above could drift from the real schema and never notice."""
    page = site.render(site.load(site.STATUS_JSON), site.load(site.CONTRACT),
                       site.load(site.CELLS), site.parse_iso(NOW))
    assert "TunaOS Package Factory" in page
    for name in site.load(site.CONTRACT)["targets"]:
        assert name in page


# --- the workflow that publishes it ----------------------------------------


WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"


def test_a_pull_request_can_render_but_never_deploy():
    """A PR must not be able to publish to the live site."""
    import yaml
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "pull_request" in flow[True]
    deploy = flow["jobs"]["deploy"]
    assert "pull_request" in deploy["if"] and "!=" in deploy["if"]


def test_it_republishes_when_the_data_it_renders_changes():
    """A schedule would race the refresh PR's merge; the push paths cannot."""
    import yaml
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    paths = set(flow[True]["push"]["paths"])
    assert "docs/factory-status.json" in paths
    assert "manifests/package-factory.yaml" in paths
    assert "scripts/render-factory-site.py" in paths


def test_the_deploy_job_asks_for_exactly_the_pages_permissions():
    import yaml
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert flow["permissions"] == {"contents": "read"}
    assert flow["jobs"]["deploy"]["permissions"] == {
        "pages": "write", "id-token": "write"}


def test_a_deploy_in_flight_is_never_cancelled():
    import yaml
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert flow["concurrency"]["cancel-in-progress"] is False


def test_pages_enables_itself_so_publishing_needs_no_settings_click():
    """The workflow is the whole publish path. If the first deploy needs a
    human to flip a toggle in repository settings first, the site is not
    actually automated -- and the failure it produces ("Get Pages site failed")
    reads like a bug rather than a missing setting."""
    import yaml
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = flow["jobs"]["deploy"]["steps"]
    configure = [s for s in steps if "configure-pages" in s.get("uses", "")]
    assert configure, "no configure-pages step -- the first deploy will fail"
    assert configure[0]["with"]["enablement"] is True
    assert steps.index(configure[0]) < next(
        i for i, s in enumerate(steps) if "deploy-pages" in s.get("uses", ""))
