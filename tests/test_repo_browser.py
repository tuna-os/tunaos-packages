"""The package browser: what repo.tunaos.org is actually serving.

docs/factory-status.json answers "how much of the plan is built". It cannot
answer "what is in the repository", and that is the question someone has when
they are deciding whether to point a machine at repo.tunaos.org -- previously
answerable only by adding the repo and asking dnf, i.e. by trusting it first
in order to find out what it holds.

The properties below are the ones that make the browser trustworthy rather
than merely present:

  * it is READ from the served indexes, never hand-kept, so it cannot list a
    package the repo does not serve nor omit one it does;
  * an index that cannot be read is REPORTED, not dropped -- a silently
    shorter list is exactly what #519 looked like from the outside;
  * a flat repo two arches point at is read ONCE, not once per arch;
  * every row is in the HTML, so the filter can only hide, never supply.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "scripts" / "snapshot-repo-contents.py"


def module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


snap = module("snapshot_repo_contents", "snapshot-repo-contents.py")
site = module("render_factory_site", "render-factory-site.py")

NOW = dt.datetime(2026, 8, 26, tzinfo=dt.timezone.utc)


def contents(**over) -> dict:
    """A snapshot in the shape the real script emits."""
    base = {
        "generated": "2026-08-26T00:00:00Z",
        "indexes": [
            {"target": "el10", "arches": ["x86_64"], "format": "rpm",
             "url": "https://repo.tunaos.org/repo/10/x86_64/", "error": None,
             "packages": [
                 {"name": "gtk4", "arch": "x86_64", "evr": "0:4.21.6-1.el10",
                  "source": "gtk4", "location": "gtk4-4.21.6-1.el10.x86_64.rpm"},
                 {"name": "libseat", "arch": "x86_64", "evr": "0:0.9.1-1.el10",
                  "source": "libseat", "location": None},
             ]},
            {"target": "ubuntu", "arches": ["amd64", "arm64"], "format": "deb",
             "url": "https://repo.tunaos.org/tideforge/ubuntu/", "error": None,
             "packages": [
                 {"name": "quickshell", "arch": "amd64", "evr": "0.2.0-1",
                  "source": "quickshell", "location": "pool/q/quickshell.deb"},
             ]},
        ],
        "totals": {"indexes": 2, "unreachable": 0, "packages": 3, "names": 3},
    }
    base.update(over)
    return base


# --- the snapshot ----------------------------------------------------------


def test_every_declared_index_is_snapshotted():
    """Contract-driven, so a target that gains a published_index appears here
    without anyone remembering to add it."""
    contract = yaml.safe_load(
        (ROOT / "manifests" / "package-factory.yaml").read_text("utf-8"))
    rows = snap.indexes(contract, None)
    declared = {
        url
        for spec in contract["targets"].values()
        for urls in (spec.get("published_index") or {}).values()
        for url in ([urls] if isinstance(urls, str) else urls)
    }
    assert declared == {row["url"] for row in rows}
    assert declared, "the contract declares no published index at all"


def test_a_flat_repo_two_arches_share_is_read_once():
    """ubuntu declares ONE URL for amd64 and arm64. Reading it per-arch would
    download it twice and report its packages twice -- the count on the page
    would be double what the repo holds."""
    contract = {"targets": {"ubuntu": {
        "format": "deb",
        "published_index": {"amd64": "https://x/ubuntu/",
                            "arm64": "https://x/ubuntu/"}}}}
    rows = snap.indexes(contract, None)
    assert len(rows) == 1
    assert rows[0]["arches"] == ["amd64", "arm64"]


def test_two_indexes_of_one_arch_stay_two():
    """The other direction: el10 x86_64 has two disjoint prefixes (#467).
    Collapsing them would hide everything the build-chain publisher writes."""
    contract = {"targets": {"el10": {
        "format": "rpm",
        "published_index": {"x86_64": ["https://x/repo/10/x86_64/",
                                       "https://x/xfce/10-stream-x86_64/"]}}}}
    assert len(snap.indexes(contract, None)) == 2


def test_a_target_with_no_published_index_contributes_none():
    """arch and opensuse-tumbleweed have none until they publish. A row with
    no URL would render as an index that 404s."""
    contract = {"targets": {"arch": {"format": "pkg.tar.zst",
                                     "published_index": None}}}
    assert snap.indexes(contract, None) == []


def test_an_unreachable_index_is_recorded_not_dropped(monkeypatch, tmp_path):
    """The single most important thing this file can report. A snapshot that
    omitted it in the name of a clean run would hide the one failure it
    exists to catch -- packages silently absent is what #519 looked like."""
    def boom(*a, **k):
        raise OSError("HTTP 502")
    monkeypatch.setattr(site, "_unused", None, raising=False)
    monkeypatch.setattr(snap.repo_index, "iter_rows", boom)
    row = snap.read({"target": "el10", "arches": ["x86_64"], "format": "rpm",
                     "url": "https://x/"}, tmp_path)
    assert row["error"] and "502" in row["error"]
    assert row["packages"] == []


def test_an_unreachable_index_does_not_fail_the_run(monkeypatch, tmp_path,
                                                    capsys):
    """One dead prefix must not take the whole site down: the page reports it
    instead."""
    monkeypatch.setattr(snap.repo_index, "iter_rows",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("dead")))
    out = tmp_path / "c.json"
    rc = snap.main(["--out", str(out), "--target", "el10",
                    "--cache", str(tmp_path / "cache")])
    assert rc == 0
    data = json.loads(out.read_text("utf-8"))
    assert data["totals"]["unreachable"] == len(data["indexes"]) > 0
    assert "::warning::" in capsys.readouterr().err


def test_source_packages_are_left_out(monkeypatch, tmp_path):
    """src rpms are build inputs; publish-rpm-wave.sh excludes them from the
    served tree, so listing them would advertise something not there."""
    monkeypatch.setattr(snap.repo_index, "iter_rows", lambda *a, **k: [
        {"name": "gtk4", "arch": "src", "evr": "0:1-1", "srpm": None},
        {"name": "gtk4", "arch": "x86_64", "evr": "0:1-1", "srpm": "gtk4-1-1.src.rpm"},
    ])
    row = snap.read({"target": "el10", "arches": ["x86_64"], "format": "rpm",
                     "url": "https://x/"}, tmp_path)
    assert [p["arch"] for p in row["packages"]] == ["x86_64"]


def test_the_snapshot_records_where_each_file_lives(monkeypatch, tmp_path):
    """Without a location the page could only GUESS the filename from the
    NEVRA -- and the publisher renames '+' out of filenames, so the guess is
    wrong for exactly the packages that already caused an incident."""
    monkeypatch.setattr(snap.repo_index, "iter_rows", lambda *a, **k: [
        {"name": "gtk4", "arch": "x86_64", "evr": "0:1-1", "srpm": None,
         "location": "gtk4-1-1.x86_64.rpm"}])
    row = snap.read({"target": "el10", "arches": ["x86_64"], "format": "rpm",
                     "url": "https://x/"}, tmp_path)
    assert row["packages"][0]["location"] == "gtk4-1-1.x86_64.rpm"


def test_the_rpm_reader_actually_captures_a_location():
    """The snapshot can only record what the reader yields. Pinned against a
    real primary.xml fragment so a refactor of repo_index cannot quietly drop
    the field and leave every download link unbuilt."""
    ri = module("repo_index_mod", "repo_index.py")
    gap = ri.gap_engine
    blob = (
        '<?xml version="1.0"?>'
        f'<metadata xmlns="{gap.COMMON[1:-1]}" xmlns:rpm="{gap.RPM[1:-1]}">'
        '<package type="rpm"><name>gtk4</name><arch>x86_64</arch>'
        '<version epoch="0" ver="4.21.6" rel="1.el10"/>'
        '<location href="gtk4-4.21.6-1.el10.x86_64.rpm"/>'
        '<format><rpm:sourcerpm>gtk4-4.21.6-1.el10.src.rpm</rpm:sourcerpm>'
        "</format></package></metadata>"
    ).encode()
    rows = list(ri.iter_rpm_rows(blob))
    assert rows[0]["location"] == "gtk4-4.21.6-1.el10.x86_64.rpm"


def test_the_snapshot_runs_end_to_end_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(snap.repo_index, "iter_rows", lambda *a, **k: [
        {"name": "p", "arch": "x86_64", "evr": "0:1-1", "srpm": None,
         "location": "p.rpm"}])
    out = tmp_path / "c.json"
    assert snap.main(["--out", str(out), "--now", "2026-08-26T00:00:00Z"]) == 0
    data = json.loads(out.read_text("utf-8"))
    assert data["generated"] == "2026-08-26T00:00:00Z"
    assert data["totals"]["packages"] == len(data["indexes"])


def test_the_snapshot_is_deterministic(monkeypatch, tmp_path):
    """Two runs over the same repository must produce the same bytes, or the
    page churns on every deploy for no reason."""
    monkeypatch.setattr(snap.repo_index, "iter_rows", lambda *a, **k: [
        {"name": "b", "arch": "x86_64", "evr": "0:1-1", "srpm": None},
        {"name": "a", "arch": "x86_64", "evr": "0:1-1", "srpm": None}])
    args = ["--out", "-", "--now", "2026-08-26T00:00:00Z"]
    first = snap.main(args) or None
    del first
    contract = yaml.safe_load(
        (ROOT / "manifests" / "package-factory.yaml").read_text("utf-8"))
    a = snap.snapshot(contract, None, tmp_path, "T")
    b = snap.snapshot(contract, None, tmp_path, "T")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    names = [p["name"] for p in a["indexes"][0]["packages"]]
    assert names == sorted(names), "rows must be sorted, not index-ordered"


# --- the rendered pages ----------------------------------------------------


CONTRACT = yaml.safe_load(
    (ROOT / "manifests" / "package-factory.yaml").read_text("utf-8"))


def overview(contract: dict | None = None, **over) -> str:
    return site.render_repo_overview(contents(**over),
                                     CONTRACT if contract is None else contract,
                                     NOW)


def index_page(which: int = 0, **over) -> str:
    data = contents(**over)
    return site.render_repo_index(site.repo_rows(data)[which], data, NOW)


def test_every_snapshotted_index_reaches_the_overview():
    page = overview()
    for row in contents()["indexes"]:
        assert row["url"] in page


def test_the_package_rows_are_in_the_html_not_generated_by_script():
    """The filter may only HIDE rows. If the list were built by script, a
    reader with scripting off would see an empty repository, and nobody could
    diff two days of the page."""
    page = index_page()
    assert "gtk4" in page and "libseat" in page
    assert "<script" not in page, "the list page body must not carry script"


def test_a_filtered_row_is_hidden_never_removed():
    """Pins the mechanism: rows carry a search key and the script toggles
    `hidden`. A script that removed rows would make the count on the page
    disagree with the repository."""
    assert 'data-k="gtk4 gtk4"' in index_page()
    assert "hidden" in site.FILTER_SCRIPT
    assert "remove" not in site.FILTER_SCRIPT


def test_the_download_link_is_built_from_the_recorded_location():
    page = index_page()
    assert ('href="https://repo.tunaos.org/repo/10/x86_64/'
            'gtk4-4.21.6-1.el10.x86_64.rpm"') in page


def test_a_package_with_no_recorded_location_still_lists():
    """A missing location costs the link, not the row. Dropping the row would
    make the page disagree with the repo about what is served."""
    assert "libseat" in index_page()


def test_an_unreachable_index_is_named_on_the_page_with_its_reason():
    data = contents(indexes=[{
        "target": "el10", "arches": ["aarch64"], "format": "rpm",
        "url": "https://repo.tunaos.org/rpm/el10/aarch64/",
        "error": "HTTPError: 502", "packages": []}])
    page = site.render_repo_overview(data, CONTRACT, NOW)
    assert "unreachable" in page
    assert "502" in page
    assert "rpm/el10/aarch64" in page


def test_an_unreachable_index_gets_no_browse_page(tmp_path):
    """There is no list to show, and a link to an empty page would read as
    'this index is empty' rather than 'this index could not be read'."""
    data = contents(indexes=[{
        "target": "el10", "arches": ["x86_64"], "format": "rpm",
        "url": "https://x/dead/", "error": "boom", "packages": []}])
    site.write_browser(data, CONTRACT, tmp_path, NOW)
    assert list((tmp_path / "repo").glob("*.html")) == []


def test_a_stale_snapshot_raises_a_visible_alarm():
    old = NOW - dt.timedelta(days=site.STALE_AFTER_DAYS + 3)
    page = overview(generated=old.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert "stale" in page
    assert "alarm" in page


def test_a_fresh_snapshot_raises_no_alarm():
    assert "stale" not in overview()


def test_the_page_says_how_to_add_the_repo():
    """A browser that shows what is served but not how to consume it stops one
    step short of the question people arrive with."""
    assert "dnf config-manager" in index_page(0)
    deb = [i for i, r in enumerate(site.repo_rows(contents()))
           if r["format"] == "deb"][0]
    assert "sources.list" in index_page(deb)


def test_each_format_the_contract_declares_has_an_add_repo_recipe():
    """Adding a format without one would render a browse page that tells a
    reader nothing about using it."""
    contract = yaml.safe_load(
        (ROOT / "manifests" / "package-factory.yaml").read_text("utf-8"))
    for spec in contract["targets"].values():
        assert spec.get("format", "rpm") in site.ADD_REPO


def test_two_indexes_of_one_target_get_distinct_pages(tmp_path):
    """el10 x86_64 has two. Naming pages by target would overwrite one with
    the other and silently halve what the browser shows."""
    data = contents(indexes=[
        {"target": "el10", "arches": ["x86_64"], "format": "rpm",
         "url": "https://repo.tunaos.org/repo/10/x86_64/", "error": None,
         "packages": [{"name": "a", "arch": "x86_64", "evr": "0:1-1",
                       "source": "a", "location": None}]},
        {"target": "el10", "arches": ["x86_64"], "format": "rpm",
         "url": "https://repo.tunaos.org/xfce/10-stream-x86_64/",
         "error": None,
         "packages": [{"name": "b", "arch": "x86_64", "evr": "0:1-1",
                       "source": "b", "location": None}]},
    ])
    site.write_browser(data, CONTRACT, tmp_path, NOW)
    assert len(list((tmp_path / "repo").glob("*.html"))) == 2


def test_the_browser_pages_are_self_contained(tmp_path):
    site.write_browser(contents(), CONTRACT, tmp_path, NOW)
    for page in [tmp_path / "packages.html", *(tmp_path / "repo").glob("*")]:
        text = page.read_text("utf-8")
        assert "<script src" not in text.lower(), page
        assert "<link" not in text.lower(), page
        for url in re.findall(r'(?:src|href)="(https?://[^"]+)"', text):
            assert url.startswith(("https://repo.", "https://github.com")), url


def test_the_status_page_links_to_the_browser_only_when_it_exists():
    """A link to a page that was not rendered is worse than no link."""
    status = json.loads(
        (ROOT / "docs" / "factory-status.json").read_text("utf-8"))
    contract = yaml.safe_load(
        (ROOT / "manifests" / "package-factory.yaml").read_text("utf-8"))
    cells = yaml.safe_load(
        (ROOT / "manifests" / "package-builds.yaml").read_text("utf-8"))
    with_browser = site.render(status, contract, cells, NOW, contents())
    without = site.render(status, contract, cells, NOW, None)
    assert 'href="packages.html"' in with_browser
    assert "packages.html" not in without


def test_rendering_the_browser_is_deterministic():
    assert overview() == overview()
    assert index_page() == index_page()


# --- the workflow that refreshes it ----------------------------------------


WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"


def flow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_deploy_takes_a_fresh_snapshot():
    """The browser's source is the live repository, not a file in git, so it
    must be read at deploy time or the page ships yesterday's repo."""
    steps = flow()["jobs"]["build"]["steps"]
    assert any("snapshot-repo-contents.py" in str(s.get("run", ""))
               for s in steps)


def test_the_snapshot_runs_before_the_render():
    steps = flow()["jobs"]["build"]["steps"]
    order = [i for i, s in enumerate(steps)
             if "snapshot-repo-contents.py" in str(s.get("run", ""))
             or "render-factory-site.py" in str(s.get("run", ""))]
    assert len(order) == 2 and order[0] < order[1]


def test_it_also_republishes_on_a_timer():
    """Unlike the status data -- which lands through a PR merge, so the push
    paths catch it -- the repository changes when a PUBLISH workflow runs, and
    no commit records that. A timer is the only signal there is."""
    triggers = flow()[True]
    assert "schedule" in triggers, (
        "without a schedule the browser only refreshes when unrelated files "
        "change on main")


def test_the_pacman_db_is_named_after_the_repo_not_the_target():
    """arch declares no published_index yet, so this is unexercised until the
    day it does -- which is exactly when a wrong db name would surface, as an
    index reported unreachable rather than as a bug anyone could see coming.

    repo-add publishes <REPO_NAME>.db (scripts/plan-arch-publish.py, "tunaos"),
    and the factory's target id is "arch". Passing the target as the db name
    would fetch arch.db and 404."""
    body = SNAPSHOT.read_text(encoding="utf-8")
    assert "repo_name=row" not in body, (
        "the target id is not the repository name; see plan-arch-publish.py")
    name = re.search(r'REPO_NAME\s*=\s*"([^"]+)"',
                     (ROOT / "scripts" / "plan-arch-publish.py")
                     .read_text(encoding="utf-8")).group(1)
    ri = module("repo_index_for_db", "repo_index.py")
    default = re.search(r'db = f"\{repo_name\}\.db" if repo_name else "([^"]+)\.db"',
                        (ROOT / "scripts" / "repo_index.py")
                        .read_text(encoding="utf-8")).group(1)
    assert default == name, (
        f"repo_index defaults to {default}.db but the publisher writes "
        f"{name}.db")
    del ri


def test_a_missing_pages_site_is_explained_not_just_failed():
    """`Create Pages site failed: Resource not accessible by integration` names
    an integration and reads like a bug. It is a one-time settings toggle that
    GITHUB_TOKEN cannot perform at any permission level, and the workflow
    should say so rather than leave the next person reading GitHub's REST
    docs (run 32926048349)."""
    steps = flow()["jobs"]["deploy"]["steps"]
    explain = [s for s in steps if s.get("if") == "failure()"]
    assert explain, "nothing explains a deploy that failed to configure Pages"
    body = str(explain[0].get("run", ""))
    assert "Settings" in body and "Pages" in body and "GitHub Actions" in body


# --- what the first live deploy exposed ------------------------------------
#
# Every issue below was measured on the published site on 2026-08-26, not
# imagined. They share one shape: the page was confidently WRONG rather than
# merely plain, which is the failure mode a status page is most prone to.


def test_a_baseurl_is_never_rendered_as_a_link():
    """All 8 index links on the live page returned 404.

    R2 serves objects and has no directory listing, so a bare prefix like
    https://repo.tunaos.org/repo/10/x86_64/ is a BASEURL you hand a package
    manager -- it is not a page and never will be. Linking it hands every
    reader a broken link and makes a working repository look dead."""
    page = index_page()
    assert '<a href="https://repo.tunaos.org/repo/10/x86_64/"' not in page
    assert "<code>https://repo.tunaos.org/repo/10/x86_64/</code>" in page


def test_the_link_that_is_offered_instead_actually_resolves():
    """repodata/repomd.xml (rpm) and InRelease (deb) DO return 200. Offering
    no link at all would be safe but useless; offering the metadata file is
    both honest and the thing a reader can inspect."""
    assert "repo/10/x86_64/repodata/repomd.xml" in index_page()
    deb = [i for i, r in enumerate(site.repo_rows(contents()))
           if r["format"] == "deb"][0]
    assert "tideforge/ubuntu/InRelease" in index_page(deb)


def test_every_format_the_contract_declares_knows_its_index_file():
    """A format without one silently falls back to no link at all."""
    for spec in CONTRACT["targets"].values():
        assert spec.get("format", "rpm") in site.INDEX_FILE


def test_a_declared_target_with_no_index_is_named_on_the_page():
    """The failure this catches, measured: opensuse-tumbleweed was serving a
    resolvable index on BOTH arches and appeared nowhere, because its
    published_index was still undeclared. The page showed five targets and
    implied that was all of them. Absence must never render as nothing."""
    page = overview()
    assert "Declared, but not listed here" in page
    for absent in ("opensuse-tumbleweed", "arch", "fedora", "hummingbird"):
        if absent not in {r["target"] for r in contents()["indexes"]}:
            assert absent in page, absent


def test_that_section_disappears_when_every_target_is_listed():
    """It is a gap report, not furniture."""
    tiny = {"targets": {"el10": {"format": "rpm"},
                        "ubuntu": {"format": "deb"}}}
    assert "Declared, but not listed here" not in overview(contract=tiny)


def test_it_does_not_claim_an_undeclared_target_is_empty():
    """The distinction that matters: "we cannot read it" is not "it has
    nothing". Tumbleweed had 21 names per arch while invisible here."""
    page = overview()
    assert "not the same as having nothing published" in page.lower() or \
        "NOT the same as having nothing published" in page


def test_one_target_arch_appearing_twice_is_explained():
    """el10 x86_64 occupies two rows identical in every visible column but
    the URL (#467). Unexplained, that reads as a duplicate-row bug."""
    data = contents(indexes=[
        {"target": "el10", "arches": ["x86_64"], "format": "rpm",
         "url": "https://repo.tunaos.org/repo/10/x86_64/", "error": None,
         "packages": []},
        {"target": "el10", "arches": ["x86_64"], "format": "rpm",
         "url": "https://repo.tunaos.org/xfce/10-stream-x86_64/",
         "error": None, "packages": []},
    ])
    page = site.render_repo_overview(data, CONTRACT, NOW)
    assert "el10 x86_64 appears more than once" in page
    assert "not duplicates" in page


def test_no_such_note_when_nothing_repeats():
    assert "appears more than once" not in overview()


def test_counts_are_formatted_for_reading():
    """9966 is a number someone reads, not an identifier."""
    page = overview(totals={"indexes": 8, "unreachable": 0,
                            "packages": 9966, "names": 8215})
    assert "9,966" in page and "8,215" in page
    assert ">9966<" not in page


def test_both_halves_of_the_site_stamp_time_the_same_way():
    """The status half wrote "2026-08-25 05:09 UTC" and the browser half the
    raw ISO string. On two linked pages that reads as two different clocks."""
    assert site.stamp("2026-08-26T04:27:40Z") == "2026-08-26 04:27 UTC"
    assert "2026-08-26T04:27:40Z" not in overview()


def test_a_stamp_it_cannot_parse_is_shown_rather_than_crashing():
    assert site.stamp("who knows") == "who knows"


def test_turning_signature_checking_off_is_justified_not_just_done():
    """The recipes carry gpgcheck=0 / trusted=yes on a repository that IS
    signed -- the apt InRelease holds a real PGP signature. The reason is
    that no public key is published at a URL to verify against. A page that
    prints the insecure line without saying so teaches it as normal."""
    page = index_page()
    assert "gpgcheck=0" in page
    lowered = page.lower()
    assert "signature checking off" in lowered
    assert "are signed" in lowered
    assert "public key is not published" in lowered


def test_long_urls_wrap_at_boundaries_not_mid_token():
    """break-all split every index URL mid-token in the coverage cards
    (".../202511" / "24-aarch64/"), which is unreadable and looks corrupt."""
    css = site.style()
    assert "word-break:break-all" not in css
    assert "overflow-wrap:anywhere" in css
