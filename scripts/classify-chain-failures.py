#!/usr/bin/env python3
"""Turn a wave's red packages into one blocker per ROOT CAUSE.

## Why

A build-chain wave reports failures as a flat list. The 2026-08-28 GNOME 51
wave reported eight:

    gtk4, libadwaita, mutter, gnome-shell, nautilus,
    xdg-desktop-portal-gnome, gnome-control-center, gnome-initial-setup

There was one failure. gtk4 could not build against pango 1.57, and the other
seven build against gtk4 or mutter-devel and never had a chance. Reading that
took a human twenty minutes and three log fetches, and the same shape recurs
every wave -- a single wall at the bottom of a tier and a fan of dependents
above it, indistinguishable from eight independent bugs until someone reads
eight logs.

That distinction is the difference between a queue an agent can work and a
list nobody can triage. So it is computed, from evidence, here:

  * ROOT      -- the failure log names no other package that also failed in
                 this wave. Something is actually wrong with this package.
  * CASCADE   -- the log's unsatisfied capability, missing provider or error
                 text names a package that ALSO failed in this wave. Nothing
                 is wrong with it that fixing the root will not fix; it heals
                 on the next wave for free.

Only roots become blockers. Cascades are recorded against their root so the
count is honest ("1 blocker, 7 dependents") rather than deleted.

## Classes

Each root is classified against the failure text, by the classes this factory
has actually paid for. The class is what decides whether a fix is mechanical
(an agent can write it) or a decision (a human owes an answer), which is the
autonomy boundary docs/rfc/rfc012 draws:

    chain-infra                     the builder died, not the package
    spec-changelog                  %changelog entries under %autochangelog
    unconditional-test-buildrequires  a dist-git spec whose test-only
                                    BuildRequires carry no %bcond, so the
                                    chain's `--without check` is inert
    version-blocked                 a provider exists, at a version that
                                    satisfies nothing
    unsatisfied-buildrequires       nothing provides the capability at all
    patch-rejected                  a carried patch stopped applying
    compile-error                   the build reached the compiler and lost
    no-output                       built, produced no RPM
    unclassified                    none of the above matched -- reported as
                                    such, never guessed at

`unclassified` is a first-class outcome. A classifier that forces every
failure into a bucket is how "python-aiohappyeyeballs failed because of the
changelog" gets asserted from log-line proximity, which is a mistake this
repo has already made in writing.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

# Ordered: the first pattern that matches wins, so the specific classes sit
# above the general ones. `error:` matches almost every failing build, so
# compile-error must come last of the substantive classes.
CLASSES: tuple[tuple[str, str, str], ...] = (
    (
        "chain-infra",
        r"Temporary repodata directory .* already exists"
        r"|createrepo_c .* failed"
        r"|cannot clone: Permission denied"
        r"|Failed to download metadata for repo"
        r"|No space left on device",
        "the builder failed, not the package",
    ),
    (
        "spec-changelog",
        r"%autochangelog.*%changelog|changelog not in descending order"
        r"|Ignoring extra changelog|bogus date in %changelog",
        "the spec mixes hand-written changelog entries with %autochangelog",
    ),
    (
        "patch-rejected",
        # GNU patch says both "Hunk #1 FAILED at 10." and "2 out of 5 hunks
        # FAILED"; build-chain.sh's own why-pattern matches only the first,
        # which is how a rejected patch can read as a bare compile error.
        r"hunks? FAILED|Patch .* does not apply|can't find file to patch",
        "a carried patch no longer applies to the imported source",
    ),
    (
        "version-blocked",
        r"but none of the providers can be installed"
        r"|package .* requires .*, but none of the providers"
        r"|cannot install both",
        "a provider exists at a version that satisfies nothing",
    ),
    (
        "unsatisfied-buildrequires",
        r"nothing provides|No match for argument|Unable to find a match"
        r"|Failed to resolve",
        "no repository in the buildroot provides the capability",
    ),
    (
        "no-output",
        r"No RPMs produced",
        "the build reported success and produced no package",
    ),
    (
        "compile-error",
        r"Bad exit status from|error:|\berror\b:.*\[-Werror",
        "the build reached the compiler and failed there",
    ),
)

# A package the chain skipped or failed is named in the summary like:
#   - src/gnome-51/gtk4
_SUMMARY_FAILURE = re.compile(r"^\s*-\s+(\S+/)?(?P<name>[\w.+-]+)\s*$")


def failed_packages(summary: str) -> list[str]:
    """Names from a chain summary's `Failed packages (N):` block."""
    names: list[str] = []
    inside = False
    for line in summary.splitlines():
        if re.search(r"Failed packages \(\d+\)", line):
            inside = True
            continue
        if inside:
            match = _SUMMARY_FAILURE.match(line)
            if match:
                names.append(match["name"])
                continue
            if line.strip():
                inside = False
    return list(dict.fromkeys(names))


def read_logs(logs_dir: pathlib.Path | None, package: str) -> str:
    """Every kept log for one package, concatenated.

    build-chain.sh copies `<package>.build.log` and `<package>.root.log` into
    the failure-logs directory. Both matter: resolution failures land in
    root.log and compile failures in build.log, and classifying from only one
    of them mis-classes whichever half it cannot see.
    """
    if not logs_dir or not logs_dir.is_dir():
        return ""
    parts = []
    for path in sorted(logs_dir.glob(f"{package}.*.log")):
        try:
            parts.append(path.read_text(errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def classify(text: str) -> tuple[str, str, str]:
    """(class, why, evidence line) for one package's failure text."""
    for name, pattern, why in CLASSES:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            line = next(
                (
                    line.strip()
                    for line in text.splitlines()
                    if re.search(pattern, line, re.IGNORECASE)
                ),
                match.group(0).strip(),
            )
            return name, why, line[:400]
    return (
        "unclassified",
        "no known failure class matched -- read the log rather than guess",
        "",
    )


def _test_only(text: str) -> bool:
    """A test-only BuildRequires the chain's --without check cannot disable.

    The chain always passes `--without check --without tests`. On a spec that
    declares no matching %bcond that flag "defines a macro nothing reads" --
    build-chain.sh says so itself -- so an unconditional
    BuildRequires: %{py3_dist pytest-asyncio} is unavoidable, and the failure
    is a packaging decision rather than a missing dependency.
    """
    return bool(
        re.search(
            r"nothing provides.*(pytest|nose|tox|catch2|googletest|gtest"
            r"|check-devel|cmocka)",
            text,
            re.IGNORECASE,
        )
    )


def cascade_root(text: str, failed: set[str], package: str) -> str | None:
    """The failed sibling this package's log blames, if any.

    Asked per sibling rather than by parsing capabilities out of the text.
    Parsing was tried first and got `pkgconfig(gtk4) >= 4.23` wrong -- it
    captured `pkgconfig`, so seven of the eight failures in the 2026-08-28
    wave read as independent roots. Matching a KNOWN name against the four
    spellings a requirement actually takes cannot make that mistake: there is
    no name to mis-extract, only a name to find.
    """
    for other in sorted(failed - {package}, key=len, reverse=True):
        name = re.escape(other)
        # The name must END where it is written. Without the lookahead,
        # `\bgtk4\b` matches inside `gtk4-layer-shell` -- `-` is a word
        # boundary -- so a package waiting on gtk4-layer-shell would be
        # attributed to a failed gtk4 it has nothing to do with, and the
        # blocker it actually needs would never be raised. The optional
        # subpackage suffixes are the spellings a BuildRequires legitimately
        # takes for the same source package.
        boundary = rf"\b{name}(?:-devel|-static|-libs|-tools|-headers)?(?![\w.+-])"
        spellings = (
            # nothing provides gtk4 / gtk4-devel >= 4.23 / pkgconfig(gtk4)
            rf"nothing provides[^\n]*{boundary}",
            rf"No match for argument:\s*{boundary}",
            rf"Unable to find a match:[^\n]*{boundary}",
            # "requires gtk4-devel >= 4.23, but none of the providers ..."
            rf"requires[^\n]*{boundary}[^\n]*none of the providers",
        )
        for spelling in spellings:
            if re.search(spelling, text, re.IGNORECASE):
                return other
    return None


def analyse(summary: str, logs_dir: pathlib.Path | None) -> dict:
    return analyse_names(
        failed_packages(summary), logs_dir, fallback=summary
    )


def analyse_names(packages: list[str], logs_dir: pathlib.Path | None,
                  fallback: str = "") -> dict:
    packages = list(dict.fromkeys(packages))
    failed = set(packages)
    records: list[dict] = []
    for package in packages:
        text = read_logs(logs_dir, package) or fallback
        if not text:
            # No log and no summary text: the package was never reached, so
            # there is nothing to classify. Saying so is the point -- an
            # unreached package is work the wave ran out of clock for, not a
            # defect, and calling it one would send an agent to fix nothing.
            records.append({
                "package": package,
                "kind": "root",
                "blocked_by": None,
                "class": "not-reached",
                "why": "no failure log: the wave never built this package",
                "evidence": "",
            })
            continue
        root = cascade_root(text, failed, package)
        if root:
            records.append({
                "package": package,
                "kind": "cascade",
                "blocked_by": root,
                "class": "cascade",
                "why": f"fails because {root} failed; heals when {root} does",
                "evidence": "",
            })
            continue
        name, why, evidence = classify(text)
        if name == "unsatisfied-buildrequires" and _test_only(text):
            name = "unconditional-test-buildrequires"
            why = (
                "a test-only BuildRequires with no %bcond guard, so the "
                "chain's --without check cannot disable it"
            )
        records.append({
            "package": package,
            "kind": "root",
            "blocked_by": None,
            "class": name,
            "why": why,
            "evidence": evidence,
        })

    roots = [r for r in records if r["kind"] == "root"]
    cascades = [r for r in records if r["kind"] == "cascade"]
    for record in roots:
        record["dependents"] = sorted(
            c["package"] for c in cascades if c["blocked_by"] == record["package"]
        )
    # `not-reached` is NOT a blocker. It is a package the wave ran out of
    # clock before attempting, and counting it as one is how a residue of 101
    # unserved names reports "101 blockers" and buries the seven that decide
    # whether an image can boot. Nothing is wrong with an unreached package;
    # the next wave builds it.
    blockers = [r for r in roots if r["class"] != "not-reached"]
    unreached = [r for r in roots if r["class"] == "not-reached"]
    return {
        "failed": len(packages),
        "blockers": len(blockers),
        "dependents": len(cascades),
        "unreached": len(unreached),
        "records": records,
    }


def render(report: dict) -> str:
    lines = [
        f"{report['failed']} package(s) outstanding: {report['blockers']} "
        f"blocker(s), {report['dependents']} dependent(s) that heal for free, "
        f"{report.get('unreached', 0)} not yet reached.",
        "",
    ]
    for record in report["records"]:
        if record["kind"] != "root" or record["class"] == "not-reached":
            continue
        lines.append(f"### {record['package']} — `{record['class']}`")
        lines.append("")
        lines.append(record["why"])
        if record["evidence"]:
            lines.append("")
            lines.append("```")
            lines.append(record["evidence"])
            lines.append("```")
        if record["dependents"]:
            lines.append("")
            lines.append(
                "Blocks: " + ", ".join(f"`{d}`" for d in record["dependents"])
            )
        lines.append("")
    unreached = [
        r for r in report["records"] if r.get("class") == "not-reached"
    ]
    if unreached:
        lines.append(
            f"Not yet reached ({len(unreached)}) — no failure log, so the "
            "wave never built them; the next one will: "
            + ", ".join(f"`{r['package']}`" for r in unreached[:25])
            + (" …" if len(unreached) > 25 else "")
        )
        lines.append("")
    unclassified = [
        r for r in report["records"]
        if r.get("class") == "unclassified"
    ]
    if unclassified:
        lines.append(
            "Unclassified (read the log; do not infer a cause from "
            "proximity): "
            + ", ".join(f"`{r['package']}`" for r in unclassified)
        )
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--summary", type=pathlib.Path,
        help="A chain log (or its tail) carrying the Failed packages block.",
    )
    source.add_argument(
        "--packages", type=pathlib.Path,
        help="A file of package names, one per line -- the convergence "
             "loop's `remaining` list. Preferred over --summary when it is "
             "available: it comes from the published index rather than from "
             "scraping a job log, so it cannot miss a package whose failure "
             "scrolled out of the tail, and it includes packages no shard "
             "ever reached.",
    )
    parser.add_argument(
        "--failure-logs", type=pathlib.Path,
        help="Directory of <package>.build.log / <package>.root.log kept by "
             "build-chain.sh. Without it every package is classified from "
             "the summary alone, which can only ever find the coarse classes.",
    )
    parser.add_argument("--json", type=pathlib.Path)
    parser.add_argument("--markdown", type=pathlib.Path)
    args = parser.parse_args(argv)

    if args.packages:
        names = [
            line.strip()
            for line in args.packages.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        report = analyse_names(names, args.failure_logs)
    else:
        report = analyse(
            args.summary.read_text(errors="replace"), args.failure_logs
        )
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")
    text = render(report)
    if args.markdown:
        args.markdown.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
