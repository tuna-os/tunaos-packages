#!/usr/bin/env python3
"""Find every buildroot resolution failure at once, without building anything.

The chain discovers failures serially: a run costs up to six hours and shows
only the failures in the tiers it reaches. dconf blocked publishing for five
days; fixing it exposed abseil-cpp/dejavu-fonts/inih a full run later. All
four were RESOLUTION failures -- knowable from repo metadata alone -- and
each cost a run to find. This tool finds the whole class in one local pass.

Why scripts/preflight-buildrequires.py could not catch them (each gap is one
of the classes this factory has paid for):

  RICH DEPS SKIPPED   it treats `(...)` requirements as satisfied. dconf died
                      on exactly one: gobject-introspection-devel's
                      `(python(abi) = 3.15 if python3)`.
  UNION OF PROVIDES   it resolves against target UNION reference. The real
                      buildroot applies dnf PRIORITIES: hummingbird's python3
                      at priority 10 MASKS Rawhide's, so `python(abi) = 3.15`
                      has no surviving provider. A union cannot see masking.
  FETCHABILITY        79 RPMs in the factory prefix are stored under
                      '+'-containing filenames; librepo requests the
                      percent-encoded path and the R2 worker serves raw paths,
                      so they 404 (publish-rpm-wave.sh:31). Resolution says
                      fine; download fails.

This simulator models all three:

  * the repo set is parsed from the SAME mock config the build uses
    (mock/hummingbird-ci.cfg): baseurl, priority, includepkgs, exclude[pkgs].
    The stock-config include supplies Rawhide; its concrete baseurl here is
    the dl.fedoraproject.org tree gap_engine already uses as the reference.
  * dnf priority masking: a package NAME present in a lower-numbered repo
    drops all instances of that name from higher-numbered repos.
  * boolean dependencies are parsed and evaluated (if/unless/and/or/with/
    without, else-forms); unparseable ones are reported as UNVERIFIED, never
    silently passed or failed.
  * instances whose location href percent-encodes differently from its raw
    form are HEAD-checked as librepo would request them; a 404 removes the
    instance and is reported.

Approximations, stated rather than hidden: co-installability conflicts and
file-deps beyond primary.xml's file subset are not modeled (both are reported
as UNVERIFIED where they decide an outcome); provider choice assumes ANY
surviving provider will do; an unversioned provide satisfies a versioned
requirement (optimistic, matching gap_engine's stance -- the goal is few
false BLOCKED, and every finding is verified by reading before acting).

Validation modes, so the tool can re-discover the known failures from first
principles before being trusted on unknown ones:

  --drop-repo tunaos-hummingbird   reproduces the pre-#548 world: dconf and
                                   libuser must come out BLOCKED.
  --no-excludes                    reproduces the pre-#551 world: the
                                   mingw/libsigc++ instances must come out
                                   unfetchable, blocking their dependents.

Usage:
  scripts/simulate-buildroot-resolution.py --order build-order-hummingbird-desktops.yml \
      [--cache DIR] [--drop-repo NAME] [--no-excludes] [--arch x86_64]
"""
from __future__ import annotations

import argparse
import collections
import fnmatch
import gzip
import importlib.util
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

HERE = pathlib.Path(__file__).resolve().parent
COMMON = "{http://linux.duke.edu/metadata/common}"
RPM = "{http://linux.duke.edu/metadata/rpm}"
REPOMD = "{http://linux.duke.edu/metadata/repo}"
FLAG_OPS = {"EQ": "=", "GE": ">=", "LE": "<=", "GT": ">", "LT": "<"}

# Rawhide, pinned to the master mirror: the source index is still the
# BuildRequires: reference (the recipes are Rawhide dist-git), and the binary
# index is what a rawhide-template mock config would inherit.  The buildroot
# itself has been Fedora 44 plus Hummingbird since 2026-09-02
# (template_repos below reads which off the config).
RAWHIDE = "https://dl.fedoraproject.org/pub/fedora/linux/development/rawhide/Everything/$basearch/os/"
RAWHIDE_SRC = "https://dl.fedoraproject.org/pub/fedora/linux/development/rawhide/Everything/source/tree/"
F44 = "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/$basearch/os/"
F44_UPDATES = "https://dl.fedoraproject.org/pub/fedora/linux/updates/44/Everything/$basearch/"
METALINK_BASEURL = {
    "fedora-44": F44,
    "updates-released-f44": F44_UPDATES,
}


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vercmp = load_module("rpm_vercmp")


# --------------------------------------------------------------------------
# Repo set, from the same file mock reads.

def template_repos(cfg_path: pathlib.Path) -> list[dict]:
    """The repos the mock config INHERITS from its include()d template.

    mock/hummingbird-ci.cfg includes the fedora-44 template and rewrites its
    [fedora]/[updates] sections in place (priority 50, ruby-default-gems
    excluded) -- see the config and
    tests/test_hummingbird_buildroot_is_fedora44_plus_hummingbird.py.  Those
    sections never appear literally in the file, so parse_mock_repos cannot
    see them; this models what the rewrite produces.  A rawhide include is
    still understood, at the template's default priority, so the simulator
    describes whatever the config actually says rather than what it used to.
    """
    text = cfg_path.read_text(encoding="utf-8")
    match = re.search(r"include\('/etc/mock/(fedora-(?:\d+|rawhide))-[a-z0-9_]+\.cfg'\)", text)
    release = match.group(1) if match else "fedora-rawhide"
    if release == "fedora-rawhide":
        return [{"id": "fedora", "baseurl": RAWHIDE, "priority": 99,
                 "includepkgs": [], "excludepkgs": []}]
    priority = 99
    excludes: list[str] = []
    rewrite = re.search(
        r'_section \+ "\\npriority=(\d+)\\n(?:metadata_expire=[^\\]*\\n)?"\s*"excludepkgs=([^\\]*)\\n"',
        text)
    if rewrite:
        priority = int(rewrite.group(1))
        excludes = _patterns(rewrite.group(2))
    number = release.split("-", 1)[1]
    return [
        {"id": "fedora", "baseurl": METALINK_BASEURL[f"fedora-{number}"],
         "priority": priority, "includepkgs": [], "excludepkgs": list(excludes)},
        {"id": "updates", "baseurl": METALINK_BASEURL[f"updates-released-f{number}"],
         "priority": priority, "includepkgs": [], "excludepkgs": list(excludes)},
    ]


def parse_mock_repos(cfg_path: pathlib.Path) -> list[dict]:
    """The dnf repo sections embedded in a mock config, as dicts."""
    text = cfg_path.read_text(encoding="utf-8")
    repos = []
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        header = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", stripped)
        if header:
            current = {"id": header.group(1)}
            repos.append(current)
            continue
        if current is None or "=" not in stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        current[key.strip()] = value.strip()
    out = []
    for repo in repos:
        if repo.get("enabled") != "1":
            continue
        baseurl = repo.get("baseurl", "")
        if not baseurl and "metalink" in repo:
            match = re.search(r"repo=([A-Za-z0-9-]+)", repo["metalink"])
            baseurl = METALINK_BASEURL.get(match.group(1), "") if match else ""
        if not baseurl or baseurl.startswith("file://"):
            continue  # local-build holds this run's own output; modeled via the build set
        out.append({
            "id": repo["id"],
            "baseurl": baseurl,
            "priority": int(repo.get("priority", "99")),
            "includepkgs": _patterns(repo.get("includepkgs")),
            "excludepkgs": _patterns(repo.get("excludepkgs")) + _patterns(repo.get("exclude")),
        })
    return out


def _patterns(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p for p in re.split(r"[,\s]+", raw) if p]


# --------------------------------------------------------------------------
# Fetching and parsing.

def fetch_primary(baseurl: str, cache: pathlib.Path) -> pathlib.Path:
    """Download (with cache) a repo's primary.xml, return a decompressed file."""
    cache.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9.-]+", "_", baseurl)
    out = cache / f"{key}.primary.xml"
    if out.exists() and out.stat().st_size > 0:
        return out
    # curl rather than urllib: this environment's egress proxy 403s
    # urllib's requests while curl (proxy-aware, CA bundle configured)
    # passes. Same reason gap_engine calls out to the network sparingly.
    import subprocess

    def get(url: str) -> bytes:
        done = subprocess.run(["curl", "-sfL", "--max-time", "600", url],
                              capture_output=True)
        if done.returncode != 0:
            raise SystemExit(f"fetch failed ({done.returncode}): {url}")
        return done.stdout

    repomd = get(baseurl + "repodata/repomd.xml")
    root = ET.fromstring(repomd)
    href = None
    for data in root.findall(f"{REPOMD}data"):
        if data.get("type") == "primary":
            href = data.find(f"{REPOMD}location").get("href")
    if href is None:
        raise SystemExit(f"{baseurl}: repomd.xml lists no primary")
    blob = get(baseurl + href)
    if href.endswith(".gz"):
        blob = gzip.decompress(blob)
    elif href.endswith((".zst", ".zck", ".xz")):
        import subprocess
        tool = {"zst": "zstd", "zck": "zstd", "xz": "xz"}[href.rsplit(".", 1)[1]]
        blob = subprocess.run([tool, "-dc"], input=blob, capture_output=True,
                              check=True).stdout
    out.write_bytes(blob)
    return out


def parse_repo(primary: pathlib.Path, repo: dict, arch: str) -> list[dict]:
    """Package instances from one repo, with per-repo pins applied."""
    keep_arches = {arch, "noarch"}
    include = repo["includepkgs"]
    exclude = repo["excludepkgs"]
    instances = []
    for _, element in ET.iterparse(str(primary), events=("end",)):
        if element.tag != f"{COMMON}package":
            continue
        name = element.findtext(f"{COMMON}name")
        pkg_arch = element.findtext(f"{COMMON}arch")
        if (pkg_arch not in keep_arches
                or name.endswith(("-debuginfo", "-debugsource"))
                or (include and not any(fnmatch.fnmatch(name, p) for p in include))
                or (exclude and any(fnmatch.fnmatch(name, p) for p in exclude))):
            element.clear()
            continue
        version = element.find(f"{COMMON}version")
        evr = f"{version.get('epoch') or '0'}:{version.get('ver')}-{version.get('rel')}"
        provides = []
        for entry in element.findall(f"{COMMON}format/{RPM}provides/{RPM}entry"):
            ever = entry.get("ver")
            pevr = (f"{entry.get('epoch') or '0'}:{ever}"
                    + (f"-{entry.get('rel')}" if entry.get("rel") else "")
                    ) if ever else None
            provides.append((sys.intern(entry.get("name")), pevr))
        requires = []
        for entry in element.findall(f"{COMMON}format/{RPM}requires/{RPM}entry"):
            rname = entry.get("name")
            op = FLAG_OPS.get(entry.get("flags") or "")
            rver = entry.get("ver")
            revr = (f"{entry.get('epoch') or '0'}:{rver}"
                    + (f"-{entry.get('rel')}" if entry.get("rel") else "")
                    ) if rver else None
            requires.append((sys.intern(rname), op if revr else None, revr))
        files = [f.text for f in element.findall(f"{COMMON}format/{COMMON}file")]
        loc = element.find(f"{COMMON}location")
        source = element.findtext(f"{COMMON}format/{RPM}sourcerpm")
        instances.append({
            "name": sys.intern(name), "evr": evr, "repo": repo["id"],
            "priority": repo["priority"], "baseurl": repo["baseurl"],
            "href": loc.get("href") if loc is not None else "",
            "provides": provides, "requires": requires, "files": files,
            "srpm": source,
        })
        element.clear()
    return instances


# --------------------------------------------------------------------------
# The buildroot model.

class Buildroot:
    def __init__(self, instances: list[dict], build_set_srpms: set[str]):
        # dnf priority masking: the lowest priority number that carries a
        # NAME owns it; instances of that name elsewhere are dropped.
        best: dict[str, int] = {}
        for inst in instances:
            prio = best.get(inst["name"])
            if prio is None or inst["priority"] < prio:
                best[inst["name"]] = inst["priority"]
        self.instances = [i for i in instances
                          if i["priority"] == best[i["name"]]]
        self.masked = len(instances) - len(self.instances)
        self.by_name: dict[str, list[dict]] = collections.defaultdict(list)
        self.providers: dict[str, list[tuple[dict, str | None]]] = collections.defaultdict(list)
        self.file_providers: dict[str, list[dict]] = collections.defaultdict(list)
        for inst in self.instances:
            self.by_name[inst["name"]].append(inst)
            self.providers[inst["name"]].append((inst, inst["evr"]))
            for cap, evr in inst["provides"]:
                if cap != inst["name"]:
                    self.providers[cap].append((inst, evr))
            for path in inst["files"]:
                self.file_providers[path].append(inst)
        # Binaries the build set itself will have produced by the time a
        # dependent tier runs (they land in local-build, priority 1).
        self.build_set_srpms = build_set_srpms
        self._installable: dict[int, bool] = {}
        self._why: dict[int, str] = {}
        self.unverified: collections.Counter = collections.Counter()

    def candidates(self, cap: str, op: str | None, evr: str | None):
        found = []
        for inst, pevr in self.providers.get(cap, ()):
            if op and evr and pevr is not None:
                try:
                    if not vercmp.satisfies(pevr, op, evr):
                        continue
                except Exception:
                    pass
            found.append(inst)
        if not found and cap.startswith("/"):
            found = list(self.file_providers.get(cap, ()))
            if not found:
                return None  # primary.xml carries only a file subset: UNVERIFIED
        return found

    def satisfiable(self, cap: str, op: str | None, evr: str | None,
                    stack: frozenset = frozenset()) -> bool | None:
        """True / False / None-for-unverified."""
        if cap.startswith("("):
            return self._rich(cap, stack)
        if cap.startswith("rpmlib("):
            return True
        found = self.candidates(cap, op, evr)
        if found is None:
            self.unverified[cap] += 1
            return None
        for inst in found:
            ok = self.installable(inst, stack)
            if ok or ok is None:
                return ok if ok is not None else None if len(found) == 1 else True
        # No installable provider among survivors. One escape hatch: a binary
        # the build set itself produces (it will exist in local-build).
        for inst, _ in self.providers.get(cap, ()):
            src = inst.get("srpm") or ""
            stem = src[:-len(".src.rpm")] if src.endswith(".src.rpm") else src
            if stem.rsplit("-", 2)[0] in self.build_set_srpms:
                return True
        return False if found or self.providers.get(cap) else False

    def installable(self, inst: dict, stack: frozenset = frozenset()) -> bool | None:
        key = id(inst)
        if key in self._installable:
            return self._installable[key]
        if key in stack:
            return True  # dependency cycle: coinductively installable
        stack = stack | {key}
        verdict: bool | None = True
        for cap, op, evr in inst["requires"]:
            ok = self.satisfiable(cap, op, evr, stack)
            if ok is False:
                culprit = None
                for cand in (self.candidates(cap, op, evr) or []):
                    if self.installable(cand, stack) is False:
                        culprit = cand
                        break
                self._why[key] = (f"{cap}{f' {op} {evr}' if op else ''}", culprit)
                verdict = False
                break
            if ok is None and verdict is True:
                verdict = None
        self._installable[key] = verdict
        return verdict

    def why(self, inst: dict) -> str:
        entry = self._why.get(id(inst))
        return entry[0] if entry else ""

    def chain(self, inst: dict, limit: int = 12) -> list[str]:
        """Walk the uninstallability to its root: pkg needs X; X's provider
        needs Y; ... A depth-1 report blamed cmake for every Qt package when
        the shared root was one capability several hops down."""
        steps, seen = [], set()
        while inst is not None and id(inst) not in seen and len(steps) < limit:
            seen.add(id(inst))
            entry = self._why.get(id(inst))
            if entry is None:
                break
            cap, culprit = entry
            steps.append(f"{inst['name']}-{inst['evr']} [{inst['repo']}] needs {cap}"
                         + ("" if culprit is not None else " (NO surviving provider)"))
            inst = culprit
        return steps

    # Boolean dependencies (rpm.org rich deps), evaluated approximately.
    def _rich(self, expr: str, stack: frozenset) -> bool | None:
        parsed = parse_rich(expr)
        if parsed is None:
            self.unverified[expr] += 1
            return None
        return self._eval(parsed, stack)

    def _eval(self, node, stack) -> bool | None:
        kind = node[0]
        if kind == "leaf":
            _, cap, op, evr = node
            return self.satisfiable(cap, op, evr, stack)
        if kind in ("and", "with"):
            results = [self._eval(child, stack) for child in node[1]]
            if any(r is False for r in results):
                return False
            return None if any(r is None for r in results) else True
        if kind in ("or", "without"):  # `without` approximated by its left arm
            results = [self._eval(child, stack) for child in node[1]]
            if any(r is True for r in results):
                return True
            return None if any(r is None for r in results) else False
        if kind == "if":
            _, then, cond, otherwise = node
            c = self._eval(cond, stack)
            if c is True:
                return self._eval(then, stack)
            if c is False:
                return True if otherwise is None else self._eval(otherwise, stack)
            return None
        if kind == "unless":
            _, then, cond, otherwise = node
            c = self._eval(cond, stack)
            if c is False:
                return self._eval(then, stack)
            if c is True:
                return True if otherwise is None else self._eval(otherwise, stack)
            return None
        return None


def rich_tokens(expr: str) -> list[str]:
    """Structural parens vs parens INSIDE capability names.

    `python(abi)`, `pkgconfig(glib-2.0)` and friends carry parentheses as part
    of the capability. Splitting on every paren shredded exactly the
    expression this tool exists for -- dconf's
    `(python(abi) = 3.15 if python3)` -- so: split on whitespace first, treat
    LEADING parens as structural opens, and strip a trailing paren as a
    structural close only while it exceeds the opens inside the word.
    """
    tokens: list[str] = []
    for chunk in expr.split():
        while chunk.startswith("("):
            tokens.append("(")
            chunk = chunk[1:]
        closers = 0
        while chunk.endswith(")") and chunk.count("(") < chunk.count(")"):
            chunk = chunk[:-1]
            closers += 1
        if chunk:
            tokens.append(chunk)
        tokens.extend(")" * closers)
    return tokens


def parse_rich(expr: str):
    """(name [op ver] {and|or|if|unless|with|without} ... [else ...]) -> tree."""
    tokens = rich_tokens(expr)
    pos = 0

    def parse_group():
        nonlocal pos
        if tokens[pos] != "(":
            return None
        pos += 1
        left = parse_operand()
        if left is None:
            return None
        while pos < len(tokens) and tokens[pos] != ")":
            keyword = tokens[pos]
            pos += 1
            right = parse_operand()
            if right is None:
                return None
            if keyword in ("and", "or", "with", "without"):
                base = keyword if keyword in ("and", "or") else \
                    ("and" if keyword == "with" else "or")
                if left[0] == base:
                    left[1].append(right)
                else:
                    left = (base, [left, right])
            elif keyword in ("if", "unless"):
                otherwise = None
                if pos < len(tokens) and tokens[pos] == "else":
                    pos += 1
                    otherwise = parse_operand()
                    if otherwise is None:
                        return None
                left = (keyword, left, right, otherwise)
            else:
                return None
        if pos >= len(tokens) or tokens[pos] != ")":
            return None
        pos += 1
        return left

    def parse_operand():
        nonlocal pos
        if pos < len(tokens) and tokens[pos] == "(":
            return parse_group()
        if pos >= len(tokens) or tokens[pos] == ")":
            return None
        name = tokens[pos]
        pos += 1
        if (pos + 1 < len(tokens) and tokens[pos] in ("=", ">=", "<=", ">", "<")):
            op, ver = tokens[pos], tokens[pos + 1]
            pos += 2
            if ":" not in ver:
                ver = "0:" + ver
            return ("leaf", name, op, ver)
        return ("leaf", name, None, None)

    tree = parse_group()
    return tree if tree is not None and pos == len(tokens) else None


# --------------------------------------------------------------------------
# Fetchability: request each surviving instance the way librepo would.

def unfetchable(instances: list[dict], testable_hosts: tuple[str, ...]) -> list[dict]:
    suspects = []
    for inst in instances:
        href = inst["href"]
        if not href or not inst["baseurl"].startswith(testable_hosts):
            continue
        if urllib.parse.quote(href, safe="/") != href:
            suspects.append(inst)
    bad = []

    import subprocess

    def head(inst):
        url = inst["baseurl"] + urllib.parse.quote(inst["href"], safe="/")
        done = subprocess.run(
            ["curl", "-sfI", "--max-time", "30", "-o", "/dev/null", url],
            capture_output=True)
        return None if done.returncode == 0 else inst

    with ThreadPoolExecutor(max_workers=16) as pool:
        for result in pool.map(head, suspects):
            if result is not None:
                bad.append(result)
    return bad


# --------------------------------------------------------------------------

def order_sources(order_path: pathlib.Path) -> list[str]:
    import yaml
    spec = yaml.safe_load(order_path.read_text(encoding="utf-8"))
    names = []
    for tier in spec.get("tiers", []):
        for pkg in tier.get("packages", []):
            path = pkg.get("path", "")
            names.append(pkg.get("distgit") or path.rsplit("/", 1)[-1])
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", default="build-order-hummingbird-desktops.yml")
    ap.add_argument("--mock-config", default="mock/hummingbird-ci.cfg")
    ap.add_argument("--cache", default=str(pathlib.Path.home() / ".cache/buildroot-sim"))
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--drop-repo", action="append", default=[],
                    help="simulate this repo absent (validation mode)")
    ap.add_argument("--no-excludes", action="store_true",
                    help="ignore exclude= lines (validation mode)")
    ap.add_argument("--skip-fetch-check", action="store_true")
    ap.add_argument("--json", help="also write machine-readable results here")
    args = ap.parse_args()
    root = HERE.parent
    cache = pathlib.Path(args.cache)

    repos = parse_mock_repos(root / args.mock_config)
    repos = [r for r in repos if r["id"] not in args.drop_repo]
    if args.no_excludes:
        for repo in repos:
            repo["excludepkgs"] = []
    repos.extend(template_repos(root / args.mock_config))
    for repo in repos:
        repo["baseurl"] = repo["baseurl"].replace("$basearch", args.arch).replace("$arch", args.arch)

    # The three F44 pin sections share one baseurl (and their -updates twins
    # share another). Parsing per SECTION scanned the same ~700MB XML six
    # times; group by baseurl and union the includepkgs pins instead -- all
    # six sit at the same priority, so the union is semantically identical.
    grouped: dict[str, dict] = {}
    for repo in repos:
        merged = grouped.setdefault(repo["baseurl"], {
            "id": repo["id"], "baseurl": repo["baseurl"],
            "priority": repo["priority"], "includepkgs": [], "excludepkgs": []})
        merged["priority"] = min(merged["priority"], repo["priority"])
        if repo["includepkgs"]:
            merged["includepkgs"].extend(repo["includepkgs"])
        else:
            merged["unpinned"] = True
        merged["excludepkgs"].extend(repo["excludepkgs"])
    for merged in grouped.values():
        if merged.pop("unpinned", False):
            merged["includepkgs"] = []

    print(f"repos: {[(r['id'], r['priority']) for r in grouped.values()]}",
          file=sys.stderr)
    instances = []
    for repo in grouped.values():
        print(f"  fetching {repo['id']} ...", file=sys.stderr, flush=True)
        primary = fetch_primary(repo["baseurl"], cache)
        got = parse_repo(primary, repo, args.arch)
        print(f"  {repo['id']}: {len(got)} instances", file=sys.stderr, flush=True)
        instances.extend(got)

    sources = order_sources(root / args.order)
    build_set = set(sources)

    bad = [] if args.skip_fetch_check else unfetchable(
        instances, ("https://repo.tunaos.org/",))
    if bad:
        gone = {id(b) for b in bad}
        instances = [i for i in instances if id(i) not in gone]
        print(f"\nUNFETCHABLE ({len(bad)}) -- advertised but 404 as librepo requests them:")
        for inst in sorted(bad, key=lambda i: i["href"])[:20]:
            print(f"  {inst['repo']}: {inst['href']}")
        if len(bad) > 20:
            print(f"  ... and {len(bad) - 20} more")

    br = Buildroot(instances, build_set)
    print(f"masked by priority: {br.masked} instances", file=sys.stderr)

    src_primary = fetch_primary(RAWHIDE_SRC, cache)
    build_requires: dict[str, list] = {}
    for _, element in ET.iterparse(str(src_primary), events=("end",)):
        if element.tag != f"{COMMON}package":
            continue
        name = element.findtext(f"{COMMON}name")
        if name in build_set:
            reqs = []
            for entry in element.findall(f"{COMMON}format/{RPM}requires/{RPM}entry"):
                op = FLAG_OPS.get(entry.get("flags") or "")
                ver = entry.get("ver")
                evr = (f"{entry.get('epoch') or '0'}:{ver}"
                       + (f"-{entry.get('rel')}" if entry.get("rel") else "")
                       ) if ver else None
                reqs.append((entry.get("name"), op if evr else None, evr))
            build_requires[name] = reqs
        element.clear()

    blocked, unverified_only, missing_srpm = {}, {}, []
    for source in sources:
        reqs = build_requires.get(source)
        if reqs is None:
            missing_srpm.append(source)
            continue
        bad_reqs, iffy = [], []
        for cap, op, evr in reqs:
            ok = br.satisfiable(cap, op, evr)
            if ok is False:
                found = br.candidates(cap, op, evr) or []
                chain = []
                for inst in found:
                    if br.installable(inst) is False:
                        chain = br.chain(inst)
                        break
                bad_reqs.append((f"{cap}{f' {op} {evr}' if op else ''}", chain))
            elif ok is None:
                iffy.append(cap)
        if bad_reqs:
            blocked[source] = bad_reqs
        elif iffy:
            unverified_only[source] = iffy

    print(f"\n==> {len(sources)} sources: {len(sources) - len(blocked) - len(unverified_only) - len(missing_srpm)} OK, "
          f"{len(blocked)} BLOCKED, {len(unverified_only)} unverified-only, "
          f"{len(missing_srpm)} not in Rawhide source index")
    roots: dict[str, list[str]] = collections.defaultdict(list)
    for source in sorted(blocked):
        for cap, chain in blocked[source]:
            roots[chain[-1] if chain else f"{cap} (NO surviving provider)"].append(source)
    print(f"\n==> {len(roots)} DISTINCT ROOT CAUSES, worst first:")
    for root_cause, sources in sorted(roots.items(), key=lambda kv: -len(set(kv[1]))):
        hit = sorted(set(sources))
        print(f"\nROOT ({len(hit)} pkgs): {root_cause}")
        print(f"    e.g. {', '.join(hit[:8])}" + (" ..." if len(hit) > 8 else ""))
    print()
    for source in sorted(blocked):
        print(f"BLOCKED {source}")
        for cap, chain in blocked[source]:
            print(f"    needs {cap}")
            for step in chain:
                print(f"      -> {step}")
    if unverified_only:
        print(f"\nUNVERIFIED-ONLY (file-deps beyond primary.xml or exotic rich deps): "
              f"{', '.join(sorted(unverified_only)[:15])}"
              + (" ..." if len(unverified_only) > 15 else ""))
    if missing_srpm:
        print(f"\nNOT IN RAWHIDE SOURCE INDEX (local-only specs, BRs unchecked): "
              f"{', '.join(sorted(missing_srpm))}")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({
            "blocked": {s: [{"needs": c, "chain": ch} for c, ch in v]
                        for s, v in blocked.items()},
            "unverified_only": unverified_only,
            "missing_srpm": missing_srpm,
            "unfetchable": [b["href"] for b in bad]}, indent=2))
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
