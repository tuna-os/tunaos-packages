#!/usr/bin/env python3
"""Render the factory's own measurements into a browsable page.

`docs/FACTORY-STATUS.md` answers "what has the factory built" for someone
already reading the repository. Nothing answers it for someone who is not:
the contract lives in `manifests/package-factory.yaml`, the measurement in
`docs/factory-status.json`, the build cells in `manifests/package-builds.yaml`,
and reconciling the three is a job people were doing by hand.

This renders all three into one self-contained page for GitHub Pages. It is a
VIEW, never a source: every number here is read from the artifacts the factory
already generates, so the page cannot claim a coverage the measurement does
not support. If the measurement is stale the page says so in the header rather
than presenting old numbers as current -- the same reasoning as the staleness
alarm in scripts/factory-status.py.

Deliberately dependency-free output: one HTML file, styles inlined, no CDN, no
build toolchain. Pages serves it as-is and it opens from disk.

Usage:

    scripts/render-factory-site.py [--out site] [--now ISO8601]

`--now` exists so the tests can pin "days since measurement" without freezing
the clock globally.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]

STATUS_JSON = ROOT / "docs" / "factory-status.json"
CONTRACT = ROOT / "manifests" / "package-factory.yaml"
CELLS = ROOT / "manifests" / "package-builds.yaml"
REPO_CONTENTS = ROOT / "docs" / "repo-contents.json"

# How the repo is added on each format, shown on that index's browse page.
# A browser that shows you what is served but not how to consume it stops one
# step short of the question people actually arrive with.
ADD_REPO = {
    "rpm": ("dnf config-manager addrepo "
            "--set=baseurl={url} --set=name=tunaos --set=gpgcheck=0"),
    "deb": ("echo 'deb [trusted=yes] {url} ./' | "
            "sudo tee /etc/apt/sources.list.d/tunaos.list"),
    "pkg.tar.zst": "# add to /etc/pacman.conf:\n[{name}]\nServer = {url}",
}

# How old a measurement may be before the page stops presenting it as current.
# Matches the ::warning threshold in scripts/factory-status.py so the page and
# the workflow cannot disagree about what "stale" means.
STALE_AFTER_DAYS = 2

# Palette parameters, taken unchanged from the data-viz reference instance.
# Only two encodings are used and neither is categorical:
#   * built-vs-remaining is part-to-whole, so it takes the SEQUENTIAL hue
#     (one blue) against a neutral remainder -- target identity is carried by
#     the row label, never by colour, so no per-target hue is invented and
#     there is no categorical palette to validate;
#   * state takes the reserved status palette, always with an icon and a word
#     beside it so the colour never carries the meaning alone.
TOKENS_LIGHT = {
    "surface-1": "#fcfcfb",
    "surface-2": "#f0efec",
    "surface-3": "#e4e3df",
    "border": "#d9d8d3",
    "text-primary": "#0b0b0b",
    "text-secondary": "#52514e",
    "text-muted": "#77756f",
    "fill": "#2a78d6",
    "fill-soft": "#cde2fb",
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}
TOKENS_DARK = {
    "surface-1": "#1a1a19",
    "surface-2": "#242423",
    "surface-3": "#2e2e2c",
    "border": "#3a3a37",
    "text-primary": "#ffffff",
    "text-secondary": "#c3c2b7",
    "text-muted": "#96958c",
    "fill": "#3987e5",
    "fill-soft": "#184f95",
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

# Status is icon + label + colour, never colour alone.
STATE_ICONS = {
    "good": "●",
    "warning": "▲",
    "serious": "▲",
    "critical": "■",
    "muted": "○",
}


def thousands(value: int) -> str:
    """9966 is a count someone has to read, not an identifier."""
    return f"{value:,}"


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def load(path: pathlib.Path):
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def parse_iso(stamp: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(stamp)
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def stamp(value: str) -> str:
    """One date format for the whole site.

    The status half wrote "2026-08-25 05:09 UTC" and the browser half wrote
    the raw ISO string; on two linked pages that reads as two different
    clocks."""
    try:
        return parse_iso(value).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def days_between(later: dt.datetime, earlier: dt.datetime) -> int:
    return max(0, (later - earlier).days)


# The file each format's index metadata actually lives in, relative to the
# baseurl. A baseurl is what you hand a package manager; it is NOT a page --
# R2 serves objects and has no directory listing, so linking the bare prefix
# gives every reader a 404 (measured 2026-08-26: all 8 of them). These DO
# resolve, so they are what gets linked; the baseurl itself is rendered as
# copyable text.
INDEX_FILE = {
    "rpm": "repodata/repomd.xml",
    "deb": "InRelease",
    "pkg.tar.zst": "tunaos.db",
}


def baseurl(url: str, fmt: str = "rpm", names: str = "") -> str:
    """A served index, shown as copyable text plus a link that resolves."""
    meta = INDEX_FILE.get(fmt)
    tail = f' <span class="dim">{esc(names)}</span>' if names else ""
    if not meta:
        return f"<code>{esc(url)}</code>{tail}"
    href = url.rstrip("/") + "/" + meta
    return (f"<code>{esc(url)}</code> "
            f'<a class="meta" href="{esc(href)}">index</a>{tail}')


def slug(value: str) -> str:
    """A filename-safe id for an index, derived from its URL.

    Two indexes of one target differ only by URL (el10 x86_64 has two), so the
    target name alone cannot name the page.
    """
    keep = [c if c.isalnum() else "-" for c in
            value.replace("https://", "").replace("http://", "").strip("/")]
    return re.sub(r"-+", "-", "".join(keep)).strip("-").lower()


def pill(state: str, label: str) -> str:
    """A state chip: colour, icon and word together."""
    return (f'<span class="pill pill-{esc(state)}">'
            f'<span class="ico" aria-hidden="true">{STATE_ICONS[state]}</span>'
            f"{esc(label)}</span>")


def delta_text(value: int) -> str:
    """Signed movement. Zero reads as 'no change', not as '+0'."""
    if value > 0:
        return f"+{value}"
    return str(value) if value else "no change"


def bar(built: int, needed: int) -> str:
    """Part-to-whole, one hue against a neutral remainder.

    Direct-labelled rather than legended: with two segments and the numbers
    written beside the bar, a legend box would restate what the labels say.
    """
    total = built + needed
    percent = (built / total * 100) if total else 0.0
    return (
        f'<div class="bar" role="img" aria-label="{built} of {total} built">'
        f'<div class="bar-fill" style="width:{percent:.1f}%"></div>'
        f"</div>"
    )


def coverage_rows(status: dict, contract: dict | None = None) -> list[dict]:
    """One row per measured target x architecture, in a stable order."""
    formats = {t: (c or {}).get("format", "rpm")
               for t, c in ((contract or {}).get("targets") or {}).items()}
    rows = []
    trend_rows = (status.get("trend") or {}).get("rows") or {}
    for target in sorted(status.get("targets") or {}):
        for arch in sorted(status["targets"][target]):
            data = status["targets"][target][arch]
            built = len(data.get("built") or [])
            needed = len(data.get("needed") or [])
            rows.append({
                "target": target,
                "arch": arch,
                "built": built,
                "needed": needed,
                "indexes": data.get("indexes") or [],
                "format": formats.get(target, "rpm"),
                "trend": trend_rows.get(f"{target}/{arch}") or {},
            })
    return rows


def render_hero(status: dict, contract: dict, rows: list[dict],
                now: dt.datetime) -> str:
    declared = len(contract.get("targets") or {})
    measured = len({r["target"] for r in rows})
    built = sum(r["built"] for r in rows)
    needed = sum(r["needed"] for r in rows)
    measured_at = parse_iso(status["measured_at"])
    age = days_between(now, measured_at)

    stale = ""
    if age > STALE_AFTER_DAYS:
        stale = (
            f'<p class="alarm">{pill("critical", "Stale measurement")}'
            f" These numbers are {age} days old. The daily refresh is not"
            " landing, so treat every figure below as history rather than"
            " as the factory's current state.</p>"
        )

    plural = "" if age == 1 else "s"
    age_note = "" if age == 0 else f" &middot; {age} day{plural} ago"

    tiles = [
        ("Targets declared", declared, "in the factory contract"),
        ("Targets measured", measured, "serving an index that resolves"),
        ("Packages built", built, "across every measured target x arch"),
        ("Still needed", needed, "declared in a catalog, not yet served"),
    ]
    cells = "".join(
        f'<div class="tile"><div class="tile-n">{value}</div>'
        f'<div class="tile-k">{esc(label)}</div>'
        f'<div class="tile-s">{esc(sub)}</div></div>'
        for label, value, sub in tiles
    )
    return (
        f'<section class="hero"><div class="tiles">{cells}</div>{stale}'
        f'<p class="measured">Measured '
        f'<time datetime="{esc(status["measured_at"])}">'
        f'{esc(measured_at.strftime("%Y-%m-%d %H:%M UTC"))}</time>'
        f"{age_note}</p></section>"
    )


def render_coverage(rows: list[dict]) -> str:
    if not rows:
        return ""
    cards = []
    for row in rows:
        trend = row["trend"]
        badges = []
        if trend.get("new"):
            badges.append(pill("good", "newly measured"))
        for name in (trend.get("regressed") or [])[:1]:
            count = len(trend["regressed"])
            label = (f"REGRESSED: {count} name{'' if count == 1 else 's'} "
                     f"no longer served")
            badges.append(pill("critical", label))
        stalled = trend.get("stalled")
        if stalled:
            prefix = "≥ " if stalled.get("at_least") else ""
            badges.append(pill(
                "warning", f"flat {prefix}{stalled['days']} days"))
        built_delta = trend.get("built_delta")
        if built_delta:
            badges.append(pill("good" if built_delta > 0 else "serious",
                               f"built {delta_text(built_delta)}"))

        indexes = "".join(
            "<li>" + baseurl(i["baseurl"], row.get("format", "rpm"),
                             f'{i.get("package_names", 0)} names') + "</li>"
            for i in row["indexes"]
        )
        total = row["built"] + row["needed"]
        cards.append(
            f'<article class="card">'
            f'<h3>{esc(row["target"])} <span class="arch">'
            f'{esc(row["arch"])}</span></h3>'
            f'<p class="counts"><strong>{row["built"]}</strong> built'
            f'<span class="dim"> of {total}</span>'
            f'<span class="need">{row["needed"]} needed</span></p>'
            f'{bar(row["built"], row["needed"])}'
            f'<div class="badges">{"".join(badges)}</div>'
            f'{f"<ul class=idx>{indexes}</ul>" if indexes else ""}'
            f"</article>"
        )

    # A table view is not optional: it is how the same numbers stay readable
    # without colour, and how they can be copied.
    body = "".join(
        f"<tr><td>{esc(r['target'])}</td><td>{esc(r['arch'])}</td>"
        f"<td class=num>{r['built']}</td><td class=num>{r['needed']}</td>"
        f"<td class=num>{r['built'] + r['needed']}</td>"
        f"<td class=num>{delta_text(r['trend'].get('built_delta') or 0)}</td>"
        f"</tr>"
        for r in rows
    )
    table = (
        '<details class="tableview"><summary>Table view</summary>'
        "<table><thead><tr><th>Target</th><th>Arch</th><th>Built</th>"
        "<th>Needed</th><th>Catalog</th><th>Change</th></tr></thead>"
        f"<tbody>{body}</tbody></table></details>"
    )
    return (f'<section id="coverage"><h2>Coverage</h2>'
            f'<div class="grid">{"".join(cards)}</div>{table}</section>')


def render_unmeasured(status: dict) -> str:
    unmeasured = status.get("unmeasured_targets") or {}
    if not unmeasured:
        return ""
    items = "".join(
        f"<li><strong>{esc(name)}</strong> "
        f'<span class="dim">{esc(data.get("format", "?"))} &middot; '
        f'{data.get("catalog_entries", 0)} catalog entries</span><br>'
        f'<span class="reason">{esc(data.get("reason", ""))}</span></li>'
        for name, data in sorted(unmeasured.items())
    )
    return (
        '<section id="unmeasured"><h2>Declared but unmeasured</h2>'
        "<p class=lede>These targets exist in the contract and have catalog "
        "entries, but nothing here can be measured yet. They are listed so "
        "the coverage above is never mistaken for the whole picture.</p>"
        f"<ul class=gaps>{items}</ul></section>"
    )


def render_desktop(status: dict) -> str:
    coverage = status.get("desktop_coverage") or {}
    if not coverage:
        return ""
    blocks = []
    for target, data in sorted(coverage.items()):
        rows = []
        for arch, desktops in sorted((data.get("architectures") or {}).items()):
            for desktop, info in sorted(desktops.items()):
                present, roots = info.get("present", 0), info.get("roots", 0)
                missing = info.get("missing") or []
                state = "good" if present >= roots and roots else "warning"
                rows.append(
                    f"<tr><td>{esc(arch)}</td><td>{esc(desktop)}</td>"
                    f"<td class=num>{present}/{roots}</td>"
                    f"<td>{pill(state, 'complete' if state == 'good' else f'{len(missing)} missing')}"
                    f"</td></tr>"
                )
        blocks.append(
            f"<h3>{esc(target)} <span class=dim>"
            f'{esc(data.get("roots_manifest", ""))}</span></h3>'
            "<table><thead><tr><th>Arch</th><th>Desktop</th>"
            "<th>Roots</th><th>State</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    return (f'<section id="desktops"><h2>Desktop closures</h2>'
            f'{"".join(blocks)}</section>')


def render_config(contract: dict, cells: dict) -> str:
    rows = []
    for name, target in sorted((contract.get("targets") or {}).items()):
        status_name = target.get("status", "unknown")
        state = {"supported": "good"}.get(
            status_name.split()[0] if status_name else "", "muted")
        index = target.get("published_index") or {}
        served = sum(len(v) if isinstance(v, list) else 1
                     for v in index.values())
        rows.append(
            f"<tr><td><strong>{esc(name)}</strong></td>"
            f"<td>{pill(state, status_name)}</td>"
            f"<td>{esc(target.get('format', ''))}</td>"
            f"<td>{esc(target.get('repository', ''))}</td>"
            f"<td>{esc(', '.join(target.get('architectures') or []))}</td>"
            f"<td>{esc(target.get('buildroot', ''))}</td>"
            f'<td class=num>{served or "&mdash;"}</td></tr>'
        )
    targets_table = (
        "<table><thead><tr><th>Target</th><th>Status</th><th>Format</th>"
        "<th>Repository</th><th>Architectures</th><th>Buildroot</th>"
        "<th>Indexes</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )

    cell_rows = []
    for cell in sorted(cells.get("native_builds") or [],
                       key=lambda c: (c.get("family", ""), c.get("id", ""))):
        enabled = cell.get("enabled", True)
        cell_rows.append(
            f"<tr><td><code>{esc(cell.get('id', ''))}</code></td>"
            f"<td>{esc(cell.get('family', ''))}</td>"
            f"<td>{esc(cell.get('track', ''))}</td>"
            f"<td>{esc(cell.get('target', ''))}</td>"
            f"<td>{esc(cell.get('architecture', ''))}</td>"
            f"<td>{pill('good' if enabled else 'muted', 'enabled' if enabled else 'disabled')}</td>"
            f"</tr>"
        )
    cells_table = (
        "<table><thead><tr><th>Cell</th><th>Family</th><th>Track</th>"
        "<th>Target</th><th>Arch</th><th>State</th></tr></thead>"
        f"<tbody>{''.join(cell_rows)}</tbody></table>"
    )
    return (
        '<section id="config"><h2>Configuration</h2>'
        "<p class=lede>Read straight from "
        "<code>manifests/package-factory.yaml</code> and "
        "<code>manifests/package-builds.yaml</code>. If this disagrees with "
        "the coverage above, the contract and the measurement disagree.</p>"
        f"<h3>Targets</h3>{targets_table}"
        f"<h3>Native build cells</h3>{cells_table}</section>"
    )


def style() -> str:
    def block(tokens):
        return "".join(f"--{k}:{v};" for k, v in tokens.items())
    return (
        "<style>"
        f":root{{{block(TOKENS_LIGHT)}color-scheme:light;}}"
        f"@media (prefers-color-scheme:dark){{:root:not([data-theme=\"light\"])"
        f"{{{block(TOKENS_DARK)}color-scheme:dark;}}}}"
        f":root[data-theme=\"dark\"]{{{block(TOKENS_DARK)}color-scheme:dark;}}"
        "*{box-sizing:border-box}"
        "body{margin:0;background:var(--surface-1);color:var(--text-primary);"
        "font:16px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,"
        "Helvetica,Arial,sans-serif;}"
        ".wrap{max-width:1080px;margin:0 auto;padding:32px 20px 64px}"
        "h1{font-size:1.6rem;margin:0 0 4px}"
        "h2{font-size:1.15rem;margin:40px 0 12px;padding-bottom:8px;"
        "border-bottom:1px solid var(--border)}"
        "h3{font-size:1rem;margin:22px 0 8px}"
        "a{color:var(--fill)}"
        ".sub{color:var(--text-secondary);margin:0 0 8px}"
        ".lede{color:var(--text-secondary);margin:0 0 14px;max-width:70ch}"
        ".tiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,"
        "minmax(190px,1fr));margin:20px 0 10px}"
        ".tile{background:var(--surface-2);border:1px solid var(--border);"
        "border-radius:10px;padding:14px 16px}"
        ".tile-n{font-size:1.9rem;font-weight:650;letter-spacing:-.02em}"
        ".tile-k{font-weight:600;font-size:.9rem}"
        ".tile-s{color:var(--text-muted);font-size:.78rem;margin-top:2px}"
        ".measured{color:var(--text-muted);font-size:.85rem;margin:6px 0 0}"
        ".alarm{background:var(--surface-2);border:1px solid var(--critical);"
        "border-radius:10px;padding:12px 14px;margin:14px 0 0;"
        "color:var(--text-secondary);font-size:.9rem}"
        ".grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,"
        "minmax(300px,1fr))}"
        ".card{background:var(--surface-2);border:1px solid var(--border);"
        "border-radius:12px;padding:14px 16px}"
        ".card h3{margin:0 0 6px;font-size:1rem}"
        ".arch{color:var(--text-muted);font-weight:500;font-size:.85rem}"
        ".counts{margin:0 0 8px;font-size:.9rem;color:var(--text-secondary)}"
        ".counts strong{font-size:1.25rem;color:var(--text-primary)}"
        ".need{float:right;color:var(--text-muted)}"
        ".bar{height:10px;border-radius:5px;background:var(--surface-3);"
        "overflow:hidden;display:flex}"
        ".bar-fill{background:var(--fill);border-radius:5px;"
        "box-shadow:0 0 0 2px var(--surface-2)}"
        ".badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}"
        ".pill{display:inline-flex;align-items:center;gap:5px;font-size:.75rem;"
        "border-radius:999px;padding:2px 9px;background:var(--surface-3);"
        "border:1px solid var(--border);color:var(--text-secondary)}"
        ".ico{font-size:.7em;line-height:1}"
        ".pill-good .ico{color:var(--good)}.pill-warning .ico{color:var(--warning)}"
        ".pill-serious .ico{color:var(--serious)}"
        ".pill-critical{border-color:var(--critical)}"
        ".pill-critical .ico{color:var(--critical)}"
        ".pill-muted .ico{color:var(--text-muted)}"
        # overflow-wrap:anywhere breaks a long URL only where it must,
        # instead of mid-token on every line the way break-all did.
        ".idx{margin:10px 0 0;padding-left:16px;font-size:.78rem;"
        "color:var(--text-secondary);overflow-wrap:anywhere}"
        ".idx li{margin-bottom:4px}"
        "a.meta{font-size:.9em;white-space:nowrap}"
        ".dim{color:var(--text-muted)}"
        ".reason{color:var(--text-secondary);font-size:.85rem}"
        ".gaps{padding-left:18px}.gaps li{margin-bottom:10px}"
        "table{border-collapse:collapse;width:100%;font-size:.85rem;"
        "display:block;overflow-x:auto;white-space:nowrap}"
        "th,td{text-align:left;padding:7px 10px;"
        "border-bottom:1px solid var(--border)}"
        "th{color:var(--text-secondary);font-weight:600}"
        "td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}"
        "code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
        "font-size:.85em}"
        "input[type=search]{width:100%;max-width:420px;padding:8px 11px;"
        "font:inherit;font-size:.9rem;border-radius:8px;margin:4px 0 6px;"
        "border:1px solid var(--border);background:var(--surface-2);"
        "color:var(--text-primary)}"
        "pre{background:var(--surface-2);border:1px solid var(--border);"
        "border-radius:8px;padding:12px 14px;overflow-x:auto;font-size:.85rem}"
        "tr[hidden]{display:none}"
        ".tableview{margin-top:16px}"
        ".tableview summary{cursor:pointer;color:var(--text-secondary);"
        "font-size:.85rem}"
        "footer{margin-top:48px;padding-top:16px;"
        "border-top:1px solid var(--border);color:var(--text-muted);"
        "font-size:.8rem}"
        "</style>"
    )


# --- the package browser ---------------------------------------------------
#
# The status page answers "how much of the plan is built". This answers the
# different question someone has when they are deciding whether to point a
# machine at repo.tunaos.org: what is actually IN it. Both are views of a
# measurement -- these pages are rendered from docs/repo-contents.json, which
# scripts/snapshot-repo-contents.py reads from the served indexes themselves,
# at the same URLs a package manager would use.
#
# Rows are written into the HTML, never fetched or generated at view time. The
# filter box is an inline script that only HIDES rows; with scripting off the
# page is the full list, and a reader is never looking at a subset the page
# has not told them about.

FILTER_SCRIPT = (
    "<script>"
    "(function(){"
    "var box=document.getElementById('q'),"
    "rows=document.querySelectorAll('#pkgs tbody tr'),"
    "out=document.getElementById('shown');"
    "if(!box)return;"
    "box.hidden=false;"
    "box.addEventListener('input',function(){"
    "var q=box.value.trim().toLowerCase(),n=0;"
    "for(var i=0;i<rows.length;i++){"
    "var hit=!q||rows[i].getAttribute('data-k').indexOf(q)>-1;"
    "rows[i].hidden=!hit;if(hit)n++;}"
    "out.textContent=q?(n+' of '+rows.length+' shown'):"
    "(rows.length+' packages');});"
    "})();"
    "</script>"
)


def repo_rows(contents: dict) -> list[dict]:
    """Index rows in a stable order, biggest first, unreachable at the top.

    An index that could not be read is the most important row on the page, so
    it sorts above the ones that worked rather than below them.
    """
    rows = []
    for entry in contents.get("indexes", []):
        rows.append(dict(entry, slug=slug(entry["url"]),
                         count=len(entry.get("packages") or [])))
    rows.sort(key=lambda r: (r["error"] is None, r["count"]), reverse=True)
    return rows


def multi_index_note(rows: list[dict]) -> str:
    """Say why one target/arch can occupy two rows.

    el10 x86_64 does (#467): two publishers write to disjoint prefixes. With
    every other column identical, the two rows otherwise read as a bug."""
    seen, repeated = set(), set()
    for row in rows:
        for arch in row["arches"] or [""]:
            key = (row["target"], arch)
            (repeated if key in seen else seen).add(key)
    if not repeated:
        return ""
    which = ", ".join(f"{t} {a}".strip() for t, a in sorted(repeated))
    return (f'<p class="lede">{which} appears more than once, and that is '
            "correct: two publishers write to disjoint prefixes, so one "
            "target and arch can have several indexes (#467). They are "
            "different repositories, not duplicates &mdash; the "
            "<em>Index</em> column is what tells them apart.</p>")


def repo_freshness(contents: dict, now: dt.datetime) -> tuple[int, str]:
    """Age of the snapshot in days, and the sentence that states it."""
    generated = contents.get("generated")
    if not generated:
        return 0, "Snapshot time not recorded."
    age = days_between(now, parse_iso(generated))
    when = f"Read from the live indexes on {esc(stamp(generated))}"
    if age == 0:
        return age, f"{when} (today)."
    return age, f"{when} &mdash; {age} day{'' if age == 1 else 's'} ago."


def render_missing_indexes(contents: dict, contract: dict) -> str:
    """Declared targets this listing cannot show, and why.

    Absence is the one thing a browser must never render as nothing. On
    2026-08-26 opensuse-tumbleweed was serving a resolvable index on BOTH
    arches and did not appear here at all, because its published_index was
    still undeclared -- so the page showed five targets and implied that was
    all of them. A gap is a finding; it gets a row."""
    shown = {row["target"] for row in contents.get("indexes", [])}
    missing = [t for t in sorted(contract.get("targets") or {})
               if t not in shown]
    if not missing:
        return ""
    items = "".join(
        f"<li><strong>{esc(t)}</strong> <span class=\"dim\">"
        f"{esc((contract['targets'][t] or {}).get('format', 'rpm'))}</span>"
        "</li>" for t in missing)
    return (
        "<h2>Declared, but not listed here</h2>"
        f'<p class="lede">{len(missing)} target'
        f"{'' if len(missing) == 1 else 's'} the factory contract declares "
        "have no <code>published_index</code>, so this page cannot read them. "
        "That is NOT the same as having nothing published: a target can be "
        "serving packages while its index is still undeclared, and it would "
        "be invisible here until the contract names it. Check the target "
        "before concluding it is empty.</p>"
        f'<ul class="gaps">{items}</ul>')


def render_repo_overview(contents: dict, contract: dict,
                         now: dt.datetime) -> str:
    """packages.html: every served index, with a way into each."""
    rows = repo_rows(contents)
    totals = contents.get("totals", {})
    age, freshness = repo_freshness(contents, now)
    dead = [r for r in rows if r["error"]]

    alarm = ""
    if age > STALE_AFTER_DAYS:
        alarm = (f'<p class="alarm">{pill("critical", "stale")} This listing '
                 f"is {age} days old. The repository may have changed since; "
                 "ask the index itself before relying on this page.</p>")
    if dead:
        names = ", ".join(f"{esc(r['target'])} ({esc(r['url'])})"
                          for r in dead)
        alarm += (f'<p class="alarm">{pill("critical", "unreachable")} '
                  f"{len(dead)} declared index"
                  f"{'' if len(dead) == 1 else 'es'} could not be read: "
                  f"{names}. Packages served there are missing from this "
                  "listing, and may be missing from the repository.</p>")

    body = [
        "<h1>Package browser</h1>",
        '<p class="sub">What <code>repo.tunaos.org</code> is serving right '
        "now, read from the same indexes your package manager reads.</p>",
        '<p class="lede">Every index the factory contract declares as '
        "published, and every package in it. Source packages are left out: "
        "they are build inputs, and the served tree excludes them too.</p>",
        '<div class="tiles">',
        f'<div class="tile"><div class="tile-n">'
        f'{thousands(totals.get("names", 0))}</div><div class="tile-k">package names'
        f'</div><div class="tile-s">distinct, across every index</div></div>',
        f'<div class="tile"><div class="tile-n">'
        f'{thousands(totals.get("packages", 0))}</div><div class="tile-k">packages'
        f'</div><div class="tile-s">counting each index separately</div>'
        f'</div>',
        f'<div class="tile"><div class="tile-n">{len(rows)}</div>'
        f'<div class="tile-k">served indexes</div>'
        f'<div class="tile-s">{len(dead)} unreachable</div></div>',
        "</div>",
        f'<p class="measured">{freshness}</p>',
        alarm,
        "<h2>Indexes</h2>",
        multi_index_note(rows),
        "<table><thead><tr><th>Target</th><th>Arch</th><th>Format</th>"
        '<th class="num">Packages</th><th>Index</th></tr></thead><tbody>',
    ]
    for row in rows:
        arches = ", ".join(row["arches"]) or "any"
        if row["error"]:
            count = pill("critical", "unreachable")
            link = f'<code>{esc(row["url"])}</code>'
        else:
            count = (f'<a href="repo/{esc(row["slug"])}.html">'
                     f'{thousands(row["count"])}</a>')
            link = baseurl(row["url"], row["format"])
        body.append(
            f"<tr><td>{esc(row['target'])}</td><td>{esc(arches)}</td>"
            f"<td><code>{esc(row['format'])}</code></td>"
            f'<td class="num">{count}</td><td>{link}</td></tr>')
    body.append("</tbody></table>")
    for row in dead:
        body.append(f'<p class="reason"><strong>{esc(row["target"])}</strong> '
                    f'{esc(row["url"])}: <code>{esc(row["error"])}</code></p>')
    body.append(render_missing_indexes(contents, contract))
    return "".join(body)


def render_repo_index(row: dict, contents: dict, now: dt.datetime) -> str:
    """One index's full package list."""
    _, freshness = repo_freshness(contents, now)
    packages = row.get("packages") or []
    arches = ", ".join(row["arches"]) or "any"
    recipe = ADD_REPO.get(row["format"], "")
    how = ""
    if recipe:
        how = (f"<h2>Using it</h2><pre><code>"
               f"{esc(recipe.format(url=row['url'], name=row['target']))}"
               f"</code></pre>"
               '<p class="reason">These lines turn signature checking OFF '
               "(<code>gpgcheck=0</code> / <code>trusted=yes</code>), and "
               "that is a compromise, not a recommendation. The packages and "
               "the apt <code>InRelease</code> ARE signed &mdash; but the "
               "public key is not published at a URL this page can point you "
               "at, so there is currently no way to state a verifying recipe "
               "that works. Until there is, treat this repository as one you "
               "are choosing to trust on the strength of its transport.</p>")

    body = [
        f"<h1>{esc(row['target'])} <span class=\"arch\">{esc(arches)}</span>"
        "</h1>",
        f'<p class="sub">{baseurl(row["url"], row["format"])}</p>',
        f'<p class="measured">{freshness} '
        f'<a href="../packages.html">All indexes</a> &middot; '
        f'<a href="../index.html">Factory status</a></p>',
        how,
        "<h2>Packages</h2>",
        '<input id="q" type="search" hidden placeholder="Filter by package '
        'or source name" aria-label="Filter packages">',
        f'<p class="measured" id="shown">{thousands(len(packages))} packages</p>',
        '<table id="pkgs"><thead><tr><th>Package</th><th>Version</th>'
        "<th>Arch</th><th>Source</th></tr></thead><tbody>",
    ]
    for pkg in packages:
        # Strip the always-'0:' epoch: it is noise on every row and the two
        # packages that carry a real epoch still show theirs.
        evr = pkg["evr"][2:] if pkg["evr"].startswith("0:") else pkg["evr"]
        source = pkg.get("source") or ""
        key = esc(f"{pkg['name']} {source}".lower())
        name = esc(pkg["name"])
        if pkg.get("location"):
            href = row["url"].rstrip("/") + "/" + pkg["location"].lstrip("/")
            name = f'<a href="{esc(href)}">{name}</a>'
        body.append(
            f'<tr data-k="{key}"><td>{name}</td><td><code>{esc(evr)}</code>'
            f"</td><td>{esc(pkg['arch'])}</td>"
            f'<td class="dim">{esc(source)}</td></tr>')
    body.append("</tbody></table>")
    return "".join(body)


def shell(title: str, description: str, body: str, *, depth: int = 0,
          script: str = "") -> str:
    """The page frame both the status page and the browser pages wear."""
    up = "../" * depth
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f'<meta name="description" content="{esc(description)}">'
        f"{style()}</head><body><div class=wrap>"
        f"{body}"
        "<footer>Generated by <code>scripts/render-factory-site.py</code> "
        "from <code>docs/repo-contents.json</code>, which "
        "<code>scripts/snapshot-repo-contents.py</code> reads from the served "
        "indexes themselves. Nothing here is a hand-kept list. "
        f'<a href="{up}index.html">Factory status</a> &middot; '
        '<a href="https://github.com/tuna-os/tunaos-packages">Source</a>.'
        f"</footer></div>{script}</body></html>"
    )


def render(status: dict, contract: dict, cells: dict,
           now: dt.datetime, contents: dict | None = None) -> str:
    rows = coverage_rows(status, contract)
    # Only offered when the snapshot is actually there to link to: a link to a
    # page that was not rendered is worse than no link.
    browse = ""
    if contents is not None:
        totals = contents.get("totals", {})
        # The link text is a phrase that stands on its own. It used to run
        # "Browse the N package names" straight into "repo.tunaos.org is
        # serving right now", which read as one broken sentence.
        browse = (f'<p class="lede">A different question: what is actually IN '
                  f"the repository, as opposed to how much of the plan is "
                  f'built. <a href="packages.html">Browse the '
                  f'{thousands(totals.get("names", 0))} package names '
                  f"repo.tunaos.org serves</a>.</p>")
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>TunaOS Package Factory</title>"
        '<meta name="description" content="What the TunaOS package factory '
        'has built, for which targets, and what it is still missing.">'
        f"{style()}</head><body><div class=wrap>"
        "<h1>TunaOS Package Factory</h1>"
        '<p class="sub">What the factory has built, for which targets, and '
        "what it is still missing &mdash; rendered from the factory's own "
        "measurements, not from a hand-written summary.</p>"
        f"{browse}"
        f"{render_hero(status, contract, rows, now)}"
        f"{render_coverage(rows)}"
        f"{render_unmeasured(status)}"
        f"{render_desktop(status)}"
        f"{render_config(contract, cells)}"
        "<footer>Generated by <code>scripts/render-factory-site.py</code> "
        "from <code>docs/factory-status.json</code>, "
        "<code>manifests/package-factory.yaml</code> and "
        "<code>manifests/package-builds.yaml</code>. "
        "Every figure is a measurement of a live published index; none is "
        "asserted by hand. "
        '<a href="https://github.com/tuna-os/tunaos-packages">Source</a>.'
        "</footer></div></body></html>"
    )


def write_browser(contents: dict, contract: dict, out: pathlib.Path,
                  now: dt.datetime) -> int:
    """packages.html plus one page per readable index."""
    rows = repo_rows(contents)
    (out / "packages.html").write_text(
        shell("TunaOS package browser",
              "Every package repo.tunaos.org is serving, read from the "
              "published indexes themselves.",
              render_repo_overview(contents, contract, now)),
        encoding="utf-8")
    pages = out / "repo"
    pages.mkdir(parents=True, exist_ok=True)
    written = 1
    for row in rows:
        if row["error"]:
            continue                       # named on the overview, no list
        (pages / f"{row['slug']}.html").write_text(
            shell(f"{row['target']} packages — TunaOS",
                  f"The {len(row['packages'])} packages served at "
                  f"{row['url']}.",
                  render_repo_index(row, contents, now),
                  depth=1, script=FILTER_SCRIPT),
            encoding="utf-8")
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path,
                        default=ROOT / "site")
    parser.add_argument("--status", type=pathlib.Path, default=STATUS_JSON)
    parser.add_argument("--contract", type=pathlib.Path, default=CONTRACT)
    parser.add_argument("--cells", type=pathlib.Path, default=CELLS)
    parser.add_argument("--repo-contents", type=pathlib.Path,
                        default=REPO_CONTENTS,
                        help="snapshot of the served indexes from "
                             "scripts/snapshot-repo-contents.py; the package "
                             "browser is omitted if it is absent")
    parser.add_argument("--now", default=None,
                        help="ISO 8601 instant to measure staleness against "
                             "(default: now, UTC)")
    args = parser.parse_args(argv)

    now = (parse_iso(args.now) if args.now
           else dt.datetime.now(dt.UTC))
    # Absent is a legitimate state, not an error: a checkout has no snapshot
    # until something fetches one, and the status page must still render.
    contents = (load(args.repo_contents)
                if args.repo_contents and args.repo_contents.is_file()
                else None)
    page = render(load(args.status), load(args.contract), load(args.cells),
                  now, contents)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(page, encoding="utf-8")
    if contents is not None:
        written = write_browser(contents, load(args.contract), args.out, now)
        print(f"wrote {written} package-browser pages")
    # Pages runs Jekyll over the artifact unless told not to, and Jekyll
    # silently drops files beginning with an underscore.
    (args.out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"wrote {args.out / 'index.html'} ({len(page)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
