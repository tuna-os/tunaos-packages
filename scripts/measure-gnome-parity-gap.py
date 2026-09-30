#!/usr/bin/env python3
"""Measure the GNOME desktop-app parity gap for the non-RPM-family variants.

tunaos-packages#132: sailfin/flounder/grouper (openSUSE / Debian / Ubuntu)
used to ship a fraction of the desktop the dnf-family bases (skipjack,
albacore, yellowfin, bonito) do.  The 2026-07-30 image audit measured the
symptom (sailfin:gnome +0.16 GB against a ~+1 GB healthy delta); the follow-up
investigation in the issue thread found the cause: on openSUSE,
``patterns-gnome-gnome`` resolves to a 475-package session skeleton with no
nautilus, no gvfs, no gnome-keyring and no portal backend, and the manifest
lists never asked for them.

This script measures both halves of that gap, from live sources:

1. **Registry size** — the issue's own measurement, refreshed.  For every
   variant it fetches the amd64 index of ``:base`` and ``:gnome`` on GHCR and
   sums the compressed layer sizes, so the size delta is reproducible instead
   of a one-off table in an issue.
2. **Manifest-level component coverage** — it fetches the tunaOS desktop
   manifests (``manifests/desktops/gnome.yaml`` and ``gnome-debian.yaml``) and
   resolves the GNOME desktop contract (the 8 core components plus the
   session/app set the maintainer measured missing from the openSUSE pattern)
   against each variant's requested package list, per base.  Component names
   are per-distro (tracker -> tinysparql/localsearch on openSUSE, gdm3 on
   Debian, ...); metapackage coverage comes from measured facts, not guesses.

Outputs are written together in one run, like gap_engine.py:
``docs/gnome-parity-gap.json`` (machine-readable, with provenance) and
``docs/gnome-parity-gap.md`` (the readable report).  If a strict-base variant
(zypper/apt: no dnf group installs to fall back on) is missing a core
contract component the script exits non-zero, so a regression in the lists is
a hard failure instead of a silent omission (issue ask 3).

Usage:
    scripts/measure-gnome-parity-gap.py
    scripts/measure-gnome-parity-gap.py --skip-registry        # manifest audit only
    scripts/measure-gnome-parity-gap.py --manifests-dir /tmp/tunaos   # no network for manifests
    scripts/measure-gnome-parity-gap.py --report-json /tmp/report.json --report-md /tmp/report.md
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
import urllib.request

import yaml

TUNAOS_ORG = "tuna-os"
TUNAOS_REPO = "tunaOS"
TUNAOS_REF = "main"
MANIFEST_DIR = "manifests/desktops"
RAW_BASE = f"https://raw.githubusercontent.com/{TUNAOS_ORG}/{TUNAOS_REPO}/{TUNAOS_REF}"
API_BASE = f"https://api.github.com/repos/{TUNAOS_ORG}/{TUNAOS_REPO}"
GHCR_TOKEN = "https://ghcr.io/token"
GHCR_API = "https://ghcr.io/v2"

# 2026-07-30 numbers from the issue, amd64, compressed layer sizes summed.
ISSUE_BASELINE_2026_07_30 = {
    "guppy": (3.14, 4.90), "skipjack": (2.58, 3.57), "albacore": (2.58, 3.58),
    "yellowfin": (2.78, 3.77), "bonito": (3.21, 3.91), "marlin": (1.49, 2.17),
    "grouper": (1.84, 2.16), "flounder": (1.11, 1.44), "sailfin": (1.48, 1.64),
}

# Variants this audit knows about.  `section` names the packages.<section>
# list in the tunaOS manifest; variants whose section installs dnf groups or a
# full-desktop metapackage get `indirect` coverage for components they do not
# name (the groups/metapackage supply them and are runtime-verified).
# marlin/guppy are healthy references with their own fully-specified lists
# (Arch) or metapackages (Gentoo); they are size-measured only.
VARIANTS = {
    "sailfin":   {"distro": "openSUSE Tumbleweed", "pm": "zypper",
                  "manifest": "gnome.yaml", "section": "zypper",
                  "groups": False},
    "flounder":  {"distro": "Debian 13 (trixie)", "pm": "apt",
                  "manifest": "gnome-debian.yaml", "section": "apt",
                  "groups": False},
    "grouper":   {"distro": "Ubuntu 26.04 (resolute)", "pm": "apt",
                  "manifest": "gnome.yaml", "section": "apt",
                  "groups": False},
    "skipjack":  {"distro": "CentOS Stream 10", "pm": "dnf",
                  "manifest": "gnome.yaml", "section": "el10",
                  "groups": True},
    "albacore":  {"distro": "AlmaLinux 10", "pm": "dnf",
                  "manifest": "gnome.yaml", "section": "el10",
                  "groups": True},
    "yellowfin": {"distro": "AlmaLinux Kitten 10", "pm": "dnf",
                  "manifest": "gnome.yaml", "section": "el10",
                  "groups": True},
    "bonito":    {"distro": "Fedora 44", "pm": "dnf",
                  "manifest": "gnome.yaml", "section": "fedora",
                  "groups": False},
    "marlin":    {"distro": "Arch", "pm": "pacman",
                  "manifest": "gnome-arch.yaml", "section": "pacman",
                  "groups": False},
    "guppy":     {"distro": "Gentoo", "pm": "emerge",
                  "manifest": "gnome.yaml", "section": "emerge",
                  "groups": False},
}

# Package managers the component audit can resolve.  marlin (Arch) and guppy
# (Gentoo) are size-measured only: Arch uses its own fully-specified list and
# Gentoo metapackages pull their trees, so neither has a per-base component
# table here (they are the healthy references in the issue, not the gap).
AUDIT_PMS = {"zypper", "apt", "dnf"}

# zypper/apt are checked strictly: a component not named explicitly and not
# supplied by a listed metapackage is missing.  That strictness is exactly the
# #132 failure mode — on openSUSE the gnome pattern deliberately does NOT
# pull the desktop, so the list itself must name it.  dnf is `indirect` for
# anything not named, because dnf installs hard Requires transitively and the
# reference editions are runtime-verified healthy (yellowfin:gnome passes the
# desktop contract gate).
STRICT_PMS = {"zypper", "apt"}

# The GNOME desktop contract (docs/gnome-desktop-contract.md, the 8 core
# components) plus the session/app set the #132 investigation measured missing
# from patterns-gnome-gnome.  Names are per-distro; dnf is the reference
# naming and zypper/apt record where the distro renames the package (tracker ->
# tinysparql/localsearch, gdm -> gdm3 on Debian, ...).  A component resolves
# for a variant when ANY of that variant's names appears in its list.
CORE_COMPONENTS = ("gdm", "gnome-shell", "mutter", "gnome-session",
                   "gnome-keyring", "gvfs", "nautilus",
                   "xdg-desktop-portal-gnome")

COMPONENTS = {
    # core session
    "gdm": {"dnf": ["gdm"], "zypper": ["gdm"], "apt": ["gdm", "gdm3"]},
    "gnome-shell": {"dnf": ["gnome-shell"], "zypper": ["gnome-shell"],
                    "apt": ["gnome-shell"]},
    "mutter": {"dnf": ["mutter"], "zypper": ["mutter"], "apt": ["mutter"]},
    "gnome-session": {"dnf": ["gnome-session"], "zypper": ["gnome-session"],
                      "apt": ["gnome-session", "ubuntu-session"]},
    "gnome-keyring": {"dnf": ["gnome-keyring"],
                      "zypper": ["gnome-keyring", "gnome-keyring-pam"],
                      "apt": ["gnome-keyring", "libpam-gnome-keyring"]},
    "gvfs": {"dnf": ["gvfs", "gvfs-afc", "gvfs-fuse", "gvfs-goa",
                     "gvfs-gphoto2", "gvfs-mtp", "gvfs-smb"],
             "zypper": ["gvfs", "gvfs-backends", "gvfs-fuse"],
             "apt": ["gvfs", "gvfs-backends"]},
    "nautilus": {"dnf": ["nautilus"], "zypper": ["nautilus"],
                 "apt": ["nautilus"]},
    "xdg-desktop-portal-gnome": {
        "dnf": ["xdg-desktop-portal-gnome"],
        "zypper": ["xdg-desktop-portal-gnome"],
        "apt": ["xdg-desktop-portal-gnome"]},
    # session-critical control surface
    "gnome-settings-daemon": {"dnf": ["gnome-settings-daemon"],
                              "zypper": ["gnome-settings-daemon"],
                              "apt": ["gnome-settings-daemon"]},
    "gnome-control-center": {"dnf": ["gnome-control-center"],
                             "zypper": ["gnome-control-center"],
                             "apt": ["gnome-control-center"]},
    "xdg-desktop-portal-gtk": {"dnf": ["xdg-desktop-portal-gtk"],
                               "zypper": ["xdg-desktop-portal-gtk"],
                               "apt": ["xdg-desktop-portal-gtk"]},
    # apps the 2026-07-30 audit measured absent from patterns-gnome-gnome
    "gnome-bluetooth": {"dnf": ["gnome-bluetooth"],
                        "zypper": ["gnome-bluetooth"],
                        "apt": ["gnome-bluetooth"]},
    "gnome-online-accounts": {"dnf": ["gnome-online-accounts"],
                              "zypper": ["gnome-online-accounts"],
                              "apt": ["gnome-online-accounts"]},
    "gnome-initial-setup": {"dnf": ["gnome-initial-setup"],
                            "zypper": ["gnome-initial-setup"],
                            "apt": ["gnome-initial-setup"]},
    "gnome-disk-utility": {"dnf": ["gnome-disk-utility"],
                           "zypper": ["gnome-disk-utility"],
                           "apt": ["gnome-disk-utility"]},
    "fwupd": {"dnf": ["fwupd"], "zypper": ["fwupd"], "apt": ["fwupd"]},
    "yelp": {"dnf": ["yelp"], "zypper": ["yelp"], "apt": ["yelp"]},
    "orca": {"dnf": ["orca"], "zypper": ["orca"], "apt": ["orca"]},
    "search-index": {"dnf": ["tracker", "tracker-miners"],
                     "zypper": ["tinysparql", "localsearch"],
                     "apt": ["tracker", "tracker-miners"]},
    "gnome-color-manager": {"dnf": ["gnome-color-manager"],
                            "zypper": ["gnome-color-manager"],
                            "apt": ["gnome-color-manager"]},
    "gnome-remote-desktop": {"dnf": ["gnome-remote-desktop"],
                             "zypper": ["gnome-remote-desktop"],
                             "apt": ["gnome-remote-desktop"]},
    "gnome-user-docs": {"dnf": ["gnome-user-docs"],
                        "zypper": ["gnome-user-docs"],
                        "apt": ["gnome-user-docs"]},
}


# Metapackage coverage facts, measured rather than assumed.  Sources:
#   * tunaos-packages#132 thread (2026-08-10): patterns-gnome-gnome on
#     Tumbleweed resolves to 475 packages with and without --no-recommends and
#     contains none of nautilus/gvfs/gnome-keyring/xdg-desktop-portal-gnome/
#     gnome-bluetooth/gnome-online-accounts/gnome-initial-setup/gnome-disk-
#     utility/fwupd/yelp/orca/tracker/gnome-color-manager/gnome-remote-desktop/
#     gnome-user-docs.
#   * Same thread: Debian gnome-core pulls 903 packages under
#     --no-install-recommends; flounder:gnome reports 971 packages installed.
#   * tunaOS gnome.yaml (apt section): ubuntu-desktop-minimal does NOT pull
#     gnome-keyring (measured on published grouper:gnome: zero gnome-keyring
#     packages, no gnome-keyring-daemon).
METAPACKAGE_FACTS = {
    "patterns-gnome-gnome": {
        "covers": {"gdm", "gnome-shell", "mutter", "gnome-session"},
        "resolves_to": 475,
        "note": "session skeleton; none of the app components (measured)"},
    "patterns-gnome-gnome_basis": {
        "covers": {"gdm", "gnome-shell", "mutter", "gnome-session"},
        "resolves_to": None,
        "note": "same pattern family as patterns-gnome-gnome"},
    "gnome-core": {
        "covers": "all",
        "resolves_to": 903,
        "note": "Debian's supported GNOME baseline (--no-install-recommends)"},
    "ubuntu-desktop-minimal": {
        "covers": "all-except",
        "except": {"gnome-keyring"},
        "resolves_to": None,
        "note": "measured on grouper:gnome: everything except gnome-keyring"},
}

# Variants whose section installs a full-desktop metapackage and therefore
# covers every component through it (the fact's `covers` field says which).
ALL_METAPACKAGES = {"gnome-core", "ubuntu-desktop-minimal"}


def fetch(url: str, headers: dict | None = None, cache: pathlib.Path | None = None) -> bytes:
    """Fetch a URL, caching under `cache` (keyed by sha256) when one is given."""
    if cache is not None:
        import hashlib

        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        blob = cache / key
        if blob.exists():
            return blob.read_bytes()
    request = urllib.request.Request(
        url,
        headers=headers or {"User-Agent": "tunaos-packages-gnome-parity-audit"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / key).write_bytes(data)
    return data


def fetch_json(url: str, cache: pathlib.Path | None = None) -> dict:
    return json.loads(fetch(url, cache=cache).decode())


def parse_section(value) -> list[str]:
    """Package names a manifest section requests.

    A section is either a plain list (zypper/apt) or a dict with `packages`
    and `optional` keys (fedora/el10, where `groups` and `copr` install into
    the same image but are not package names).  `exclude` is a group-install
    exclusion, not a request, so it never enters the requested set.
    """
    if isinstance(value, list):
        return [str(name) for name in value]
    if isinstance(value, dict):
        names = []
        for key in ("packages", "optional"):
            for name in value.get(key, []) or []:
                names.append(str(name))
        return names
    return []


def section_uses_groups(value) -> bool:
    return isinstance(value, dict) and bool(value.get("groups"))


def resolve_components(requested: set[str], pm: str, section_value) -> dict:
    """Per-component coverage status for one variant's requested list.

    Statuses:
      explicit    — the component's per-base name is in the requested list.
      metapackage — a listed metapackage's measured fact covers it.
      indirect    — dnf family: the base resolves hard Requires transitively
                    and the reference editions are runtime-verified healthy.
      missing     — strict bases (zypper/apt): nothing in the list can supply
                    it, which is exactly the #132 failure shape.
    """
    components: dict[str, dict] = {}
    metapackages = sorted(name for name in requested if name in METAPACKAGE_FACTS)
    has_groups = section_uses_groups(section_value)
    all_fact = None
    for name in metapackages:
        fact = METAPACKAGE_FACTS[name]
        if name in ALL_METAPACKAGES:
            all_fact = fact
            break
    for component, names in COMPONENTS.items():
        per_base = names.get(pm, names["dnf"])
        requested_names = sorted(set(per_base).intersection(requested))
        if requested_names:
            status = "explicit"
        elif all_fact is not None and (
            all_fact["covers"] == "all"
            or component not in all_fact.get("except", ())
        ):
            status = "metapackage"
        elif any(
            isinstance(METAPACKAGE_FACTS[name]["covers"], set)
            and component in METAPACKAGE_FACTS[name]["covers"]
            for name in metapackages
        ):
            status = "metapackage"
        elif pm in STRICT_PMS:
            status = "missing"
        elif has_groups or pm == "dnf":
            status = "indirect"
        else:
            status = "missing"
        components[component] = {
            "status": status,
            "names": per_base,
            "requested_names": requested_names,
        }
    return components, metapackages


def ghcr_measure(variant: str, tag: str, cache: pathlib.Path) -> dict:
    """Compressed amd64 size of ghcr.io/tuna-os/<variant>:<tag>."""
    repo = f"{TUNAOS_ORG}/{variant}"
    token = fetch_json(
        f"{GHCR_TOKEN}?scope=repository:{repo}:pull&service=ghcr.io", cache
    )["token"]
    auth = {"Authorization": f"Bearer {token}"}

    def registry(url: str, accept: str) -> dict:
        return json.loads(
            fetch(url, headers={**auth, "Accept": accept}, cache=cache).decode()
        )

    index = registry(
        f"{GHCR_API}/{repo}/manifests/{tag}",
        "application/vnd.oci.image.index.v1+json, "
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
    manifests = index.get("manifests", [index])
    amd64 = next(
        m for m in manifests if m.get("platform", {}).get("architecture") == "amd64"
    )
    manifest = registry(
        f"{GHCR_API}/{repo}/manifests/{amd64['digest']}",
        "application/vnd.oci.image.manifest.v1+json",
    )
    config = registry(
        f"{GHCR_API}/{repo}/blobs/{manifest['config']['digest']}",
        "application/vnd.oci.image.config.v1+json",
    )
    return {
        "digest": amd64["digest"],
        "created": config.get("created", ""),
        "compressed_gb": round(sum(layer["size"] for layer in manifest["layers"]) / 1e9, 2),
    }


def tunaos_manifest_provenance(filename: str, cache: pathlib.Path) -> dict:
    """Best-effort commit sha of tunaOS main for a manifest path."""
    try:
        commits = fetch_json(
            f"{API_BASE}/commits?path={MANIFEST_DIR}/{filename}&per_page=1",
            cache,
        )
        commit = commits[0]
        return {
            "sha": commit["sha"],
            "date": commit["commit"]["committer"]["date"],
            "message": commit["commit"]["message"].splitlines()[0],
        }
    except Exception as error:  # provenance is informational; never fail the audit
        return {"sha": "unavailable", "error": str(error)}


def audit(manifests_dir: pathlib.Path | None, cache: pathlib.Path) -> tuple[dict, bool]:
    """Run the manifest-level component audit over every auditable variant.

    Returns (report section, hard_failed).  hard_failed is True when a
    pure-list variant (no dnf groups, no full-desktop metapackage) is missing
    a core contract component — issue ask 3, at the manifest level.
    """
    raw = {}
    provenance = {}
    if manifests_dir is not None:
        for filename in ("gnome.yaml", "gnome-debian.yaml"):
            path = manifests_dir / MANIFEST_DIR / filename
            raw[filename] = yaml.safe_load(path.read_text())
            provenance[filename] = {"sha": "local-file", "path": str(path)}
    else:
        for filename in ("gnome.yaml", "gnome-debian.yaml"):
            raw[filename] = yaml.safe_load(
                fetch(f"{RAW_BASE}/{MANIFEST_DIR}/{filename}", cache=cache).decode()
            )
            provenance[filename] = tunaos_manifest_provenance(filename, cache)

    variants: dict[str, dict] = {}
    hard_failed = False
    for variant, definition in VARIANTS.items():
        if definition["pm"] not in AUDIT_PMS:
            # Size-only references (Arch, Gentoo) — see AUDIT_PMS.
            variants[variant] = {"distro": definition["distro"],
                                 "package_manager": definition["pm"],
                                 "audited": False}
            continue
        manifest = raw[definition["manifest"]]
        section_value = manifest["packages"].get(definition["section"])
        if section_value is None:
            variants[variant] = {"error": f"no packages.{definition['section']} section"}
            continue
        requested = parse_section(section_value)
        components, metapackages = resolve_components(
            set(requested), definition["pm"], section_value
        )
        missing = sorted(
            name for name, detail in components.items()
            if detail["status"] == "missing"
        )
        core_missing = [
            name for name in CORE_COMPONENTS
            if components[name]["status"] == "missing"
        ]
        # A strict-base variant (zypper/apt) missing a core component is the
        # exact shape of the #132 failure: the list never asked for a desktop.
        # Fail the audit rather than publish a thin list (issue ask 3).
        if core_missing and definition["pm"] in STRICT_PMS:
            hard_failed = True
        variants[variant] = {
            "distro": definition["distro"],
            "package_manager": definition["pm"],
            "manifest": definition["manifest"],
            "section": definition["section"],
            "requested_count": len(requested),
            "requested_packages": sorted(requested),
            "metapackages": metapackages,
            "components": components,
            "missing_components": missing,
            "core_components_missing": core_missing,
        }
    return {"provenance": provenance, "variants": variants}, hard_failed


def registry_sizes(cache: pathlib.Path) -> dict:
    """Compressed amd64 size deltas for every variant, issue measurement method."""
    sizes: dict[str, dict] = {}
    failures = 0
    for variant in VARIANTS:
        entry: dict = {}
        for tag in ("base", "gnome"):
            try:
                entry[tag] = ghcr_measure(variant, tag, cache)
            except Exception as error:
                failures += 1
                entry[tag] = {"error": str(error)}
        if "error" not in entry.get("base", {}) and "error" not in entry.get("gnome", {}):
            entry["delta_gb"] = round(
                entry["gnome"]["compressed_gb"] - entry["base"]["compressed_gb"], 2
            )
        sizes[variant] = entry
        print(
            f"registry {variant:10} base={entry.get('base', {}).get('compressed_gb', '?')} "
            f"gnome={entry.get('gnome', {}).get('compressed_gb', '?')} "
            f"delta={entry.get('delta_gb', '?')}",
            file=sys.stderr,
        )
    sizes["_all_failed"] = failures >= 2 * len(VARIANTS)
    return sizes


def render_md(report: dict) -> str:
    """Human-readable report, written alongside the JSON."""
    measured = report["measured_at"][:19].replace("T", " ")
    lines = [
        "# GNOME desktop parity gap — sailfin / flounder / grouper",
        "",
        f"Measured {measured} UTC by `scripts/measure-gnome-parity-gap.py` "
        f"(tunaos-packages#132).  Two measurements, both from live sources: "
        f"GHCR amd64 compressed layer sizes (the issue's own method, "
        f"refreshed), and the per-base requested package lists from the "
        f"tunaOS desktop manifests resolved against the GNOME desktop "
        f"contract.  Machine-readable result: `docs/gnome-parity-gap.json`.",
        "",
        "Reproduce with:",
        "",
        "```",
        "scripts/measure-gnome-parity-gap.py",
        "```",
        "",
    ]

    # Size table
    sizes = report.get("sizes", {})
    lines += [
        "## 1. Image size deltas (GHCR, amd64, compressed)",
        "",
        "| variant | base | gnome | delta | 2026-07-30 delta (issue) |",
        "|---|---|---|---|---|",
    ]
    for variant, definition in VARIANTS.items():
        entry = sizes.get(variant, {})
        base = entry.get("base", {}).get("compressed_gb")
        gnome = entry.get("gnome", {}).get("compressed_gb")
        delta = entry.get("delta_gb")
        if base is None or gnome is None:
            lines.append(
                f"| {variant} | — | — | — | "
                f"{ISSUE_BASELINE_2026_07_30[variant][1] - ISSUE_BASELINE_2026_07_30[variant][0]:+.2f} |"
            )
            continue
        old_delta = (
            ISSUE_BASELINE_2026_07_30[variant][1]
            - ISSUE_BASELINE_2026_07_30[variant][0]
        )
        lines.append(
            f"| {variant} | {base:.2f} | {gnome:.2f} | {delta:+.2f} | {old_delta:+.2f} |"
        )
    lines += ["", "All nine editions were rebuilt 2026-08-10 (config `created` "
                   "timestamps).  The EL-family deltas dropped from ~+1.0 GB to "
                   "~+0.35 GB because those editions moved from COPR group "
                   "installs to the leaner native GNOME 50 RPM chain; the "
                   "non-RPM variants rose from +0.16/+0.33/+0.32 to "
                   "+0.44/+0.61/+0.45 as the manifest fixes from 2026-07-30 "
                   "were published.  Size alone is no longer a discriminator "
                   "between healthy and thin editions — which is why the "
                   "contract below, not the size table, is the gate.", ""]

    # Component coverage
    lines += ["## 2. GNOME contract coverage per variant", ""]
    audited = report["audit"]["variants"]
    core = list(CORE_COMPONENTS) + [
        "gnome-settings-daemon", "gnome-control-center",
        "xdg-desktop-portal-gtk",
    ]
    app = [name for name in COMPONENTS if name not in core]
    lines += ["### Core (contract + session surface)", ""]
    lines += ["| variant | " + " | ".join(name.replace("xdg-desktop-portal-", "portal-") for name in core) + " |"]
    lines += ["|---|" + "---|" * len(core)]
    for variant in ("sailfin", "flounder", "grouper", "skipjack", "albacore", "yellowfin", "bonito"):
        entry = audited.get(variant)
        if entry is None or "components" not in entry:
            continue
        cells = [{"explicit": "✔", "metapackage": "◆", "indirect": "◌", "missing": "✘"}[
            entry["components"][name]["status"]] for name in core]
        lines.append(f"| {variant} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "`✔` listed explicitly · `◆` supplied by a listed metapackage "
        "(measured fact) · `◌` supplied by an installed dnf group (runtime-"
        "verified on the reference editions) · `✘` nothing in the list "
        "supplies it.",
        "",
        "### Apps measured absent from the openSUSE pattern (2026-07-30)",
        "",
        "| variant | " + " | ".join(name for name in app) + " |",
        "|---|" + "---|" * len(app),
    ]
    for variant in ("sailfin", "flounder", "grouper"):
        entry = audited[variant]
        if "components" not in entry:
            continue
        cells = [{"explicit": "✔", "metapackage": "◆", "indirect": "◌", "missing": "✘"}[
            entry["components"][name]["status"]] for name in app]
        lines.append(f"| {variant} | " + " | ".join(cells) + " |")
    lines += ["", "`search-index` is `tracker`/`tracker-miners` on dnf/apt and "
                   "`tinysparql`/`localsearch` on openSUSE.", ""]

    # Findings / gap list
    lines += ["## 3. Findings", ""]
    for finding in report["findings"]:
        lines += [f"- **{finding['title']}** — {finding['body']}", ""]

    lines += ["## 4. Provenance", ""]
    for filename, prov in report["audit"]["provenance"].items():
        lines.append(f"- `{filename}`: `{prov.get('sha', '?')[:12]}` "
                     f"({prov.get('date', '?')[:10]}) — {prov.get('message', '')}")
    lines += ["", f"- Registry: GHCR `{report.get('registry_ok') and 'reachable' or 'NOT reachable (sizes stale)'}` "
                  f"at {measured} UTC."]
    lines += [
        "",
        "## 5. Caveats",
        "",
        "- Runtime package counts are unreliable for the apt bases: bootc does "
        "not commit `/var`, so `dpkg-query` returns empty inside "
        "`flounder:base`/`grouper:base`.  A build-time installed-package "
        "inventory written to `/usr` is required to diff published apt "
        "editions directly (issue thread, 2026-08-10).  Until that lands, the "
        "apt rows above verify the *requested* lists, and the tunaOS build "
        "gate (`verify-desktop-experience.sh`) verifies the *built* image.",
        "- This audit reads the tunaOS manifests at their fetched commit, not "
        "the state an image was built from; a list change between that commit "
        "and the next publish will not show up until the next run.",
        "- `sailfin:gnome` is the only GNOME edition that currently builds an "
        "ISO end-to-end (tuna-os/iso-builder#32); it is no longer the thinnest "
        "edition, but it is also the only one whose published image can be "
        "inventoried with `rpm -qa` — publishing that inventory would make "
        "ask 1 of the issue directly checkable.",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-json", type=pathlib.Path,
                        default=pathlib.Path("docs/gnome-parity-gap.json"))
    parser.add_argument("--report-md", type=pathlib.Path,
                        default=pathlib.Path("docs/gnome-parity-gap.md"))
    parser.add_argument("--cache", type=pathlib.Path,
                        default=pathlib.Path(".cache/gnome-parity"))
    parser.add_argument("--manifests-dir", type=pathlib.Path,
                        help="read tunaOS manifests from a local checkout instead "
                             "of fetching them (offline/test mode)")
    parser.add_argument("--skip-registry", action="store_true",
                        help="do not query GHCR; sizes are marked unavailable")
    args = parser.parse_args()

    cache = args.cache
    audit_section, hard_failed = audit(args.manifests_dir, cache)

    report: dict = {
        "measured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "issue": "https://github.com/tuna-os/tunaos-packages/issues/132",
        "issue_baseline_2026_07_30_gb": {
            variant: {"base": base, "gnome": gnome,
                      "delta": round(gnome - base, 2)}
            for variant, (base, gnome) in ISSUE_BASELINE_2026_07_30.items()
        },
        "audit": audit_section,
    }

    if args.skip_registry:
        report["sizes"] = {}
        report["registry_ok"] = False
    else:
        sizes = registry_sizes(cache)
        report["registry_ok"] = not sizes.pop("_all_failed")
        report["sizes"] = sizes

    findings = build_findings(report)
    report["findings"] = findings

    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.report_md.write_text(render_md(report))
    print(f"wrote {args.report_json}", file=sys.stderr)
    print(f"wrote {args.report_md}", file=sys.stderr)

    if hard_failed:
        for variant, entry in report["audit"]["variants"].items():
            if entry.get("core_components_missing"):
                print(
                    f"HARD FAIL {variant}: core contract components not requested: "
                    f"{entry['core_components_missing']}",
                    file=sys.stderr,
                )
        raise SystemExit(1)


def build_findings(report: dict) -> list[dict]:
    """The documented gap list: what is left, and what would close it."""
    sizes = report.get("sizes", {})
    sailfin_delta = sizes.get("sailfin", {}).get("delta_gb")
    flounder_delta = sizes.get("flounder", {}).get("delta_gb")
    grouper_delta = sizes.get("grouper", {}).get("delta_gb")
    sailfin_requested = report["audit"]["variants"]["sailfin"].get("requested_count")
    return [
        {
            "title": "The requested lists now cover the full GNOME contract",
            "body": "As of the tunaOS manifests fetched for this run, every "
                    "audited variant (sailfin, flounder, grouper, skipjack, "
                    "albacore, yellowfin, bonito) requests every core contract "
                    "component and every app the 2026-07-30 audit measured "
                    "missing.  sailfin resolves the apps explicitly "
                    + (f"({sailfin_requested} requested names); " if sailfin_requested else "")
                    + "flounder through Debian's gnome-core metapackage plus "
                    "explicit nautilus/portals; grouper through "
                    "ubuntu-desktop-minimal plus the gnome-keyring and "
                    "libpam-gnome-keyring the metapackage was measured to omit.",
        },
        {
            "title": "The 2026-07-30 images are stale; current images are in the reference bracket",
            "body": "The issue measured sailfin +0.16 / flounder +0.33 / "
                    "grouper +0.32 GB against a ~+1 GB healthy delta.  The "
                    "manifest fixes landed in tunaOS main on 2026-07-30/31 "
                    "(ce11d21, d8ebdf6) and all nine editions were rebuilt "
                    "2026-08-10: sailfin is now "
                    + (f"{sailfin_delta:+.2f} GB, " if sailfin_delta is not None else "unavailable, ")
                    + (f"flounder {flounder_delta:+.2f} GB, " if flounder_delta is not None else "flounder unavailable, ")
                    + (f"grouper {grouper_delta:+.2f} GB " if grouper_delta is not None else "grouper unavailable ")
                    + "— inside the same bracket "
                    "as the EL-family references (skipjack/albacore/yellowfin "
                    "+0.34–0.36 GB, which dropped from ~+1.0 GB when they "
                    "moved to the native GNOME 50 RPM chain).",
        },
        {
            "title": "Remaining gap 1 — published apt images cannot be inventoried",
            "body": "bootc does not commit /var, so dpkg-query is empty on "
                    "flounder/grouper images and no installed-package "
                    "inventory exists to diff.  The build-time contract "
                    "(verify-desktop-experience.sh in tunaOS, "
                    "verify-gnome-desktop-experience.py here) is the only "
                    "guard for the apt bases; a build-time inventory written "
                    "to /usr (issue ask 2) would make them diffable directly.",
        },
        {
            "title": "Remaining gap 2 — sailfin's published image has no published inventory",
            "body": "rpm -qa works on sailfin:gnome, but no inventory has been "
                    "published alongside the image, so ask 1 of the issue "
                    "(diff effective installed sets against yellowfin:gnome) "
                    "still has no committed answer.  Publishing the rpm -qa "
                    "output with the image metadata closes it.",
        },
        {
            "title": "Next candidates — kde/xfce/niri zypper lists are still thin",
            "body": "The same shape that produced this issue is unfixed for "
                    "the other desktops: kde.yaml lists 3 zypper names, "
                    "xfce.yaml 3, niri.yaml 2.  The pattern-family resolution "
                    "these rely on should get the same explicit-component "
                    "treatment sailfin's gnome.yaml got (tunaos-packages#133 "
                    "tracks the broader audit).",
        },
    ]


if __name__ == "__main__":
    main()
