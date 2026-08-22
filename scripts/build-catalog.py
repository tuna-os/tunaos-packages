#!/usr/bin/env python3
"""Bootstrap and refresh manifests/catalog.yaml (RFC 011, Phase 0).

The catalog is the single index of every package the factory builds: name,
family, packaging payload reference, upstream provenance, targets, and
membership. Phase 0 makes it a passive, complete index — no CI behavior
changes; tests/test_catalog_completeness.py enforces that it stays in
mutual coverage with the things that actually drive builds:

  - build-order*.yml           (the tiered orders each family executes)
  - manifests/target-queues/   (the tideforge/native queues per stack)

This script COLLECTS from those sources and writes the catalog. It exists so
the 800-entry bootstrap is reproducible and so drift repairs are mechanical:
if the completeness test fails, running this script and reviewing the diff
is the fix. The catalog is still the authority the RFC describes — Phase 1
inverts the relationship by GENERATING the build orders from it — but in
Phase 0 the executed orders are the measured truth, so the catalog is
derived from them, not the other way around.

Entry identity is (name, family), not bare name: the GNOME 49/50/51 families
deliberately carry the same package names at different versions for
different R2 paths, and hummingbird rebuilds names that tideforge also
packages. Collapsing those would record a fiction.

Upstream provenance, by payload kind:
  tideforge   packages/<name>/package.yaml → version, source url, sha256
  native      src/<family>/<pkg>/*.spec    → Version:, Source0: (best-effort)
  distgit     Fedora dist-git rebuild — the distgit name IS the pin
              mechanism (the family's snapshot decides the revision), so no
              per-package version is recorded here; the gap engine resolves
              it at measure time.
"""
from __future__ import annotations

import glob
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# build-order target ids → package-factory target ids. The right-hand side
# must exist in manifests/package-factory.yaml `targets:` — the completeness
# test enforces it, which is how an order growing a target the contract does
# not declare becomes a loud failure instead of a quiet publish.
TARGET_MAP = {
    "centos-stream-10-x86_64": "el10",
    "centos-stream-10-aarch64": "el10",
    "almalinux-kitten-10-x86_64": "el10",
    "fedora-44-x86_64": "fedora",
    "hummingbird-20251124-x86_64": "hummingbird",
}

SPEC_VERSION_RE = re.compile(r"^Version:\s*(\S+)", re.MULTILINE)
SPEC_SOURCE_RE = re.compile(r"^Source0?:\s*(\S+)", re.MULTILINE)


def family_of(order_file: str) -> str:
    base = os.path.basename(order_file)
    name = base.replace("build-order-", "").replace("build-order", "").replace(
        ".yml", "").strip("-")
    return name or "gnome50"  # build-order.yml is the GNOME 50 main queue


def load_yaml(path: str):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def spec_upstream(pkg_dir: str) -> dict:
    """Best-effort Version/Source0 from a native spec directory."""
    for spec in sorted(glob.glob(os.path.join(pkg_dir, "*.spec"))):
        text = open(spec, encoding="utf-8", errors="replace").read()
        out = {}
        m = SPEC_VERSION_RE.search(text)
        if m:
            out["version"] = m.group(1)
        m = SPEC_SOURCE_RE.search(text)
        if m:
            out["source"] = m.group(1)
        if out:
            out["spec"] = os.path.relpath(spec, ROOT)
            return out
    return {}


def tideforge_upstream(recipe_dir: str) -> dict:
    manifest = os.path.join(recipe_dir, "package.yaml")
    if not os.path.isfile(manifest):
        return {}
    data = load_yaml(manifest) or {}
    out = {}
    if data.get("version") is not None:
        out["version"] = str(data["version"])
    src = data.get("source") or {}
    if src.get("url"):
        out["source"] = src["url"]
    if src.get("sha256"):
        out["sha256"] = src["sha256"]
    return out


def collect() -> dict:
    """entries[(name, family)] = entry dict (mutated in place while merging)."""
    entries: dict[tuple[str, str], dict] = {}

    def entry(name: str, family: str) -> dict:
        return entries.setdefault((name, family), {
            "name": name,
            "family": family,
            "packaging": {},
            "upstream": {},
            "targets": [],
            "membership": "runtime",
            "referenced_by": [],
        })

    # ── build-order*.yml ─────────────────────────────────────────────────
    for order_file in sorted(glob.glob(os.path.join(ROOT, "build-order*.yml"))):
        data = load_yaml(order_file)
        fam = family_of(order_file)
        rel = os.path.relpath(order_file, ROOT)
        raw_target = data.get("target", "")
        target = TARGET_MAP.get(raw_target)
        if target is None:
            print(f"ERROR: {rel} target {raw_target!r} has no TARGET_MAP entry",
                  file=sys.stderr)
            sys.exit(1)
        membership = data.get("membership", "runtime")
        for tier in data.get("tiers") or []:
            for pkg in tier.get("packages") or []:
                if "copr_name" in pkg and "path" not in pkg:
                    # Sourced from a COPR, not built here (the #391 single
                    # point of failure set) — cataloged so the exposure is
                    # enumerable, with the payload kind saying exactly what
                    # it is.
                    name = pkg["copr_name"]
                    e = entry(name, fam)
                    if target not in e["targets"]:
                        e["targets"].append(target)
                    if rel not in e["referenced_by"]:
                        e["referenced_by"].append(rel)
                    e["packaging"].setdefault("rpm", {}).setdefault(
                        "copr", name)
                    continue
                path = pkg["path"]
                name = os.path.basename(path)
                e = entry(name, fam)
                if target not in e["targets"]:
                    e["targets"].append(target)
                if rel not in e["referenced_by"]:
                    e["referenced_by"].append(rel)
                if pkg.get("build_tool"):
                    e["membership"] = "build_tool"
                elif e["membership"] == "runtime":
                    e["membership"] = membership
                rpm = e["packaging"].setdefault("rpm", {})
                if pkg.get("distgit"):
                    rpm.setdefault("distgit", pkg["distgit"])
                    e["upstream"].setdefault("distgit", pkg["distgit"])
                if os.path.isdir(os.path.join(ROOT, path)):
                    rpm.setdefault("native", path)
                    if not e["upstream"].get("version"):
                        e["upstream"].update(
                            spec_upstream(os.path.join(ROOT, path)))
                elif not pkg.get("distgit"):
                    # Neither an on-disk payload nor a distgit ref: record it
                    # honestly so the completeness test can flag it.
                    rpm.setdefault("native", path)
                    rpm["missing_on_disk"] = True

    # ── Tideforge workflow matrices ──────────────────────────────────────
    # These families' executed sets live as inline `strategy.matrix` lists in
    # the workflow files, not in a build-order file. The per-file map names
    # the default (target, format) a bare `package:` cell means; include-list
    # cells that carry their own `target:` override it.
    workflow_defaults = {
        ".github/workflows/build-tideforge-supported.yml": ("el10", "rpm"),
        ".github/workflows/build-tideforge-arch.yml": ("arch", "pkg.tar.zst"),
        ".github/workflows/publish-tideforge-debs.yml": ("ubuntu", "deb"),
    }
    for rel, (default_target, fmt) in workflow_defaults.items():
        wf_path = os.path.join(ROOT, rel)
        if not os.path.isfile(wf_path):
            continue
        wf = load_yaml(wf_path)
        fam = os.path.basename(rel).replace(".yml", "").replace(
            "build-", "").replace("publish-", "")
        cells = []
        for job in (wf.get("jobs") or {}).values():
            matrix = ((job.get("strategy") or {}).get("matrix")) or {}
            # A planner-driven matrix is the STRING "${{ fromJSON(...) }}",
            # not a mapping (#479). The executed set for such a workflow is
            # its curated dispatch default instead -- read below. Skipping
            # rather than failing keeps this reader working for the
            # workflows that still carry an inline matrix.
            if not isinstance(matrix, dict):
                continue
            for name in matrix.get("package") or []:
                cells.append((name, default_target))
            for inc in matrix.get("include") or []:
                if isinstance(inc, dict) and inc.get("package"):
                    cells.append((inc["package"],
                                  inc.get("target", default_target)))
        # publish-tideforge-debs.yml derives its matrix, so the package set
        # it executes is the `packages` dispatch input's default -- the
        # curated wave a person maintains deliberately. Same names the inline
        # matrix used to carry; only the place they are written moved.
        triggers = wf.get(True) if True in wf else wf.get("on")
        packages_input = (((triggers or {}).get("workflow_dispatch") or {})
                          .get("inputs") or {}).get("packages") or {}
        for name in str(packages_input.get("default", "")).split(","):
            name = name.strip()
            if name:
                cells.append((name, default_target))
        for name, target in cells:
            e = entry(name, fam)
            if target not in e["targets"]:
                e["targets"].append(target)
            if rel not in e["referenced_by"]:
                e["referenced_by"].append(rel)
            pk = e["packaging"].setdefault(fmt, {})
            recipe_dir = os.path.join(ROOT, "packages", name)
            if os.path.isdir(recipe_dir):
                pk.setdefault("tideforge", f"packages/{name}")
                if not e["upstream"].get("version"):
                    e["upstream"].update(tideforge_upstream(recipe_dir))
            else:
                pk["missing_on_disk"] = True

    # ── manifests/target-queues/*.yaml ───────────────────────────────────
    for queue_file in sorted(
            glob.glob(os.path.join(ROOT, "manifests/target-queues/*.yaml"))):
        data = load_yaml(queue_file)
        stack = os.path.splitext(os.path.basename(queue_file))[0]
        rel = os.path.relpath(queue_file, ROOT)
        for target, queue in (data.get("queues") or {}).items():
            fmt = queue.get("format", "rpm")
            impl = queue.get("implementation", "")
            for name in queue.get("roots") or []:
                e = entry(name, f"queue-{stack}")
                if target not in e["targets"]:
                    e["targets"].append(target)
                if rel not in e["referenced_by"]:
                    e["referenced_by"].append(rel)
                pk = e["packaging"].setdefault(fmt, {})
                recipe_dir = os.path.join(ROOT, "packages", name)
                if impl.startswith("tideforge") and os.path.isdir(recipe_dir):
                    pk.setdefault("tideforge", f"packages/{name}")
                    if not e["upstream"].get("version"):
                        e["upstream"].update(tideforge_upstream(recipe_dir))
                elif impl == "native-spec":
                    pk.setdefault("implementation", impl)

    return entries


def main() -> None:
    entries = collect()
    packages = []
    for (_, _), e in sorted(entries.items()):
        e["targets"] = sorted(e["targets"])
        e["referenced_by"] = sorted(e["referenced_by"])
        if not e["upstream"]:
            del e["upstream"]
        packages.append(e)
    doc = {
        "schema": 1,
        "about": (
            "RFC 011 Phase 0 catalog: one entry per (name, family) the "
            "factory builds. Regenerate with scripts/build-catalog.py; "
            "tests/test_catalog_completeness.py enforces mutual coverage "
            "with the build orders and target queues."
        ),
        "packages": packages,
    }
    out = os.path.join(ROOT, "manifests/catalog.yaml")

    # The repo's yamllint (extends: default) wants sequence items indented
    # under their key; yaml.safe_dump left-aligns them, which fails CI on
    # every list in an 11k-line file.
    class _IndentDumper(yaml.SafeDumper):
        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow, False)

    with open(out, "w", encoding="utf-8") as fh:
        yaml.dump(doc, fh, Dumper=_IndentDumper, sort_keys=False, width=100,
                  default_flow_style=False, allow_unicode=True)
    print(f"wrote {out}: {len(packages)} entries")


if __name__ == "__main__":
    main()
