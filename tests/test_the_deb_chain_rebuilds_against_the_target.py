"""The deb backport engine, and the two ways it could silently be wrong.

scripts/build-deb-chain.sh is the deb twin of build-chain.sh: it walks a
measured order tier by tier, rebuilding each donor-suite source package in a
TARGET-suite buildroot, accumulating results into a local apt repo so a tier
can resolve what the previous one produced.

Two properties are load-bearing, and both fail quietly rather than loudly.

1. THE DONOR MUST BE `deb-src` ONLY, NEVER `deb`.
   A binary `deb` line for the donor suite would let apt satisfy build
   dependencies from the donor. Everything would still compile and the run
   would go green -- but the packages would be linked against the donor's
   libraries and would not install on the target, because their shared-library
   dependencies resolve to versions the target does not have. That is not a
   backport; it is the donor suite with extra steps. `deb-src` carries no
   binaries and cannot do this.

2. THE LOCAL REPO MUST NOT BE PINNED ABOVE THE TARGET ARCHIVE.
   Every package the chain builds is NEWER than the target's, so apt prefers
   it at equal priority and no pin is needed. Pinning above 500 would also let
   the local repo outrank the archive for packages the chain did NOT build --
   the failure the rpm path hit at priority=999, where a served package
   outranked and replaced a base one (the glib2 Obsoletes incident, publish
   run 32405815822).

Separately: the generated order must stay OUT of the `build-order*.yml`
namespace. Two consumers glob that pattern and expect the rpm shape, where a
package is a `path` to a spec directory in this repository. A backport has no
such path -- its packaging lives in the donor suite -- so it uses `source` and
`version` instead. scripts/build-catalog.py does not skip what it cannot
understand: it looks the file's `target` up in TARGET_MAP and calls sys.exit(1)
when there is no entry, so a deb order committed under that name would hard-
fail the RFC 011 catalog builder rather than be ignored.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHAIN = ROOT / "scripts" / "build-deb-chain.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "backport-deb-chain.yml"

_spec = importlib.util.spec_from_file_location(
    "measure_deb_backport_gap", ROOT / "scripts" / "measure-deb-backport-gap.py"
)
gap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gap)


def chain_text() -> str:
    return CHAIN.read_text(encoding="utf-8")


def test_the_donor_is_added_as_sources_only():
    """Every apt line that mentions the donor must be deb-src.

    Binary `deb` lines are legitimate elsewhere -- the accumulating local repo
    is one -- so the rule is not "no deb lines", it is "no deb line that points
    at the donor".
    """
    text = chain_text()
    printf_lines = [line.strip() for line in text.splitlines() if 'printf "deb' in line]
    assert printf_lines, "expected the script to write apt source lines"

    donor_lines = [line for line in printf_lines if "DONOR_SUITE" in line or "donor_url" in line]
    assert donor_lines, "expected the donor suite to be added as a source"
    for line in donor_lines:
        assert 'printf "deb-src' in line, (
            f"a binary deb line for the donor would build the chain against "
            f"the donor suite, producing packages that install nowhere on the "
            f"target: {line}"
        )

    # The local repo is the only binary source the chain adds, and it is local.
    binary_lines = [line for line in printf_lines if 'printf "deb ' in line or 'printf "deb [' in line]
    for line in binary_lines:
        assert "file:///work/repo" in line, line


def test_the_local_repo_is_not_pinned_above_the_archive():
    text = chain_text()
    assert "chain-local.list" in text
    # No preferences file, and no Pin-Priority at all, for the chain repo.
    assert "preferences.d" not in text, (
        "the chain repo needs no pin: its packages are newer, so apt prefers "
        "them anyway, and a pin would let it outrank the archive for packages "
        "the chain did not build"
    )
    assert "Pin-Priority" not in text


def test_the_chain_reindexes_between_packages():
    """Without this a tier cannot resolve what the previous tier built, and
    the ordering the measurement computed would buy nothing."""
    text = chain_text()
    assert "dpkg-scanpackages" in text
    scan = text.index("dpkg-scanpackages")
    assert "apt-get update" in text[scan:scan + 400], (
        "re-indexing without an apt-get update leaves the new package invisible"
    )


def test_the_workflow_regenerates_the_order_rather_than_trusting_the_commit():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--build-order" in text
    assert "measure-deb-backport-gap.py" in text
    # Dispatch-only: a chain rebuild is hours of runner time.
    assert "workflow_dispatch" in text
    assert "on:\n  push" not in text
    assert "timeout-minutes: 360" in text


def test_an_empty_order_is_not_a_failure():
    """A target that has caught up is the engine working, not breaking."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "nothing to rebuild" in text
    assert "steps.empty.outputs.count != '0'" in text


def test_the_generated_order_stays_out_of_the_build_order_namespace():
    """build-catalog.py sys.exit(1)s on a target it has no TARGET_MAP entry
    for, so a deb order named build-order-*.yml hard-fails it."""
    for path in ROOT.glob("build-order*.yml"):
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for tier in spec.get("tiers") or []:
            for package in tier.get("packages") or []:
                assert "source" not in package or "path" in package, (
                    f"{path.name} carries the deb backport shape; the rpm "
                    f"consumers of build-order*.yml expect `path`"
                )
    # And the workflow must not write one into that namespace either.
    assert "--build-order 'build-order" not in WORKFLOW.read_text(encoding="utf-8")


def test_the_rendered_order_names_sources_and_exact_versions():
    entry = {
        "target_suite": "resolute",
        "donor_suites": ["stonking", "stonking-proposed"],
        "tiers": [["gtk4"], ["mutter"]],
        "packages": [
            {"source": "gtk4", "donor_version": "4.23.2+ds-1", "depth": 1},
            {"source": "mutter", "donor_version": "51~beta-1", "depth": 0},
        ],
    }
    text = gap.render_build_order("ubuntu", entry, ["mutter"])
    parsed = yaml.safe_load(text)
    assert parsed["target_suite"] == "resolute"
    assert parsed["donor_suites"] == ["stonking", "stonking-proposed"]
    assert parsed["tiers"][0]["packages"][0] == {"source": "gtk4", "version": "4.23.2+ds-1"}
    # Deepest build-dependency first: gtk4 must build before mutter.
    assert parsed["tiers"][1]["packages"][0]["source"] == "mutter"
    assert text.startswith("# GENERATED"), "a generated file must say so"


def test_the_mounted_output_path_is_absolute():
    """docker reads a RELATIVE --volume source as a NAMED VOLUME.

    The first real dispatch (run 32641183871) died on exactly this, and the
    error blames the wrong thing -- it complains about invalid characters in a
    volume name rather than saying the path was relative:

        docker: Error response from daemon: create .factory/backport-ubuntu:
        ".factory/backport-ubuntu" includes invalid characters for a local
        volume name, only "[a-zA-Z0-9][a-zA-Z0-9_.-]" are allowed.

    Everything before it worked: the measurement ran, --tier narrowed the order
    to 2 source packages, and the image pulled. Resolved inside the script so
    every caller is covered rather than only the workflow that hit it.
    """
    text = chain_text()
    resolve = text.index('out=$(cd "$out" && pwd)')
    mount = text.index('--volume "$out:/work"')
    assert resolve < mount, "the output path must be absolute before it is mounted"


def test_every_donor_suite_gets_its_own_deb_src_line():
    """Ubuntu needs the release pocket AND -proposed: an in-flight transition
    can leave a source unbuildable from the release pocket alone."""
    text = chain_text()
    assert "for donor_suite in $DONOR_SUITES" in text
    assert "DONOR_SUITES=" in text
    # Still sources only, per suite.
    assert 'printf "deb-src %s %s %s\\n"' in text


def _report_entry_keys() -> set[str]:
    """The keys measure-deb-backport-gap.py actually writes per target.

    Read out of the source rather than by running a measurement, because a
    measurement needs the network. The assignment is
    `report["targets"][name] = { ... }`, a dict of literal string keys.
    """
    import ast

    tree = ast.parse((ROOT / "scripts" / "measure-deb-backport-gap.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Subscript):
            continue
        inner = target.value
        if not (isinstance(inner, ast.Subscript) and isinstance(inner.value, ast.Name)
                and inner.value.id == "report"):
            continue
        return {
            key.value for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    raise AssertionError("could not find the `report['targets'][name] = {...}` assignment")


def test_the_workflow_summary_only_reads_keys_the_report_emits():
    """A field rename in the script must not leave the workflow reading a
    key that no longer exists.

    This is not hypothetical. `donor_suite` became `donor_suites` when Ubuntu
    gained a second donor pocket; the script, manifest and tests were all
    renamed together, and the workflow's inline step-summary snippet was not.
    Run 32650470179 measured the gap correctly -- the log carries
    `resolute <- stonking + stonking-proposed: 17 source packages, 4 tiers` --
    and then died 16 seconds in with `KeyError: 'donor_suite'` while printing
    that same sentence to the summary.

    The measurement is the expensive part and it had already succeeded. Losing
    a run to a typo'd key in the cosmetic step after it is the kind of failure
    a test costs nothing to prevent, so: whatever the workflow reads off an
    entry must be something the script puts there.
    """
    emitted = _report_entry_keys()
    assert "donor_suites" in emitted, "sanity: the renamed key is the one in use"
    read = set(re.findall(r"entry\[['\"]([A-Za-z_]+)['\"]\]", WORKFLOW.read_text()))
    assert read, "the workflow is expected to read fields off the report entry"
    unknown = read - emitted
    assert not unknown, (
        f"the workflow reads report fields the script does not emit: {sorted(unknown)}; "
        f"emitted keys are {sorted(emitted)}"
    )


def test_the_tier_input_is_documented_as_narrowing_not_resuming():
    """`--tier` selects a subset of ONE run; it cannot continue a previous one.

    The local apt repo that carries a tier's output to the next tier lives on
    the runner and dies with the job. So dispatching tier-1 after a tier-0 run
    resolves tier-1's build-deps against the target archive alone -- and that
    does not fail loudly. It builds, and produces packages linked against the
    versions the backport exists to replace.

    The workflow used to advise exactly that ("re-dispatch with --tier") as
    the remedy for hitting the timeout, which is the most likely moment for
    someone to follow it.
    """
    text = WORKFLOW.read_text()
    assert "it cannot resume one" in text
    assert "RE-DISPATCH FROM tier-0 OR WITH NO TIER" in text
    # The input's own description already says empty builds everything.
    workflow = yaml.safe_load(text)
    # PyYAML reads a bare `on:` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert "empty builds all" in triggers["workflow_dispatch"]["inputs"]["tier"]["description"]


def test_the_buildroot_has_a_usable_utf8_locale():
    """A buildroot should have locale data; a minimal Ubuntu image has none.

    That is worth doing on its own terms. It is NOT, on the evidence, the
    cause of the gnome-desktop:languages SIGABRT it was added to explain:
    run 32659338235 generated the locales and the failure is unchanged, with
    `is_utf8: FALSE` still printed by the same suite. The diagnosis was wrong;
    the change is kept because the buildroot is better with it, not because it
    fixed the thing it was aimed at.

    Exporting the variable is not the same as having the locale, which is why
    the assertion below is the load-bearing half.
    """
    text = chain_text()
    assert "locales" in text, "the locales package must be installed"
    assert "locale-gen C.UTF-8" in text
    install = text.index("build-essential devscripts dpkg-dev ca-certificates")
    generate = text.index("locale-gen C.UTF-8")
    build = text.index("apt-get build-dep")
    assert install < generate < build, (
        "the locale must exist before any package is built against it"
    )


def test_the_locale_assertion_checks_a_locale_that_had_to_be_generated():
    """The first version of this check could not fail.

    It asked `LC_ALL=C.UTF-8 locale charmap`, and C.UTF-8 is built into glibc
    -- it resolves with zero generated locales, so the assertion would have
    passed on the very image it was written to catch. en_US.UTF-8 exists only
    if locale-gen actually ran.

    An assertion that cannot fail is worse than no assertion: it reports a
    property nobody has checked.
    """
    text = chain_text()
    # The assignment, not the prose: the comment above it legitimately quotes
    # the old form to explain why it was wrong.
    assert "charmap=$(LC_ALL=en_US.UTF-8 locale charmap" in text
    assert "charmap=$(LC_ALL=C.UTF-8 locale charmap" not in text
    assert "locale-gen did not produce a usable UTF-8 locale" in text


def test_a_failing_test_suite_surfaces_its_own_log():
    """meson prints a summary line and buffers the output elsewhere.

        4/6 gnome-desktop:languages  FAIL  (exit status 134 or signal 6 SIGABRT)

    That names the test and says nothing about why. The real output goes to
    meson-logs/testlog*.txt inside the build tree, which is not uploaded --
    so two full chain runs, 1h47m each, were spent guessing at a cause the log
    could not confirm. Copy it out and print it.
    """
    text = chain_text()
    assert "testlog*.txt" in text
    assert "test-suite.log" in text, "autotools buffers the same way"
    assert "/work/logs/$source.$(basename" in text, "and it must survive the run"


def test_the_chain_body_carries_no_apostrophes():
    """The docker body is a single-quoted `bash -lc` string.

    ONE apostrophe ends it early, and bash then reports `unexpected EOF while
    looking for matching '` at the LAST line of the script -- nowhere near the
    comment that broke it. The word that did it while writing the locale fix
    above was "Ubuntu's".

    verify-package-factory-cell.sh has the same shape and the same guard.
    """
    text = chain_text()
    start = text.index("bash -lc '")
    body = text[start + len("bash -lc '"):]
    end = body.index("\n  '")
    assert "'" not in body[:end], (
        "an apostrophe inside the single-quoted bash -lc body ends it early; "
        "bash will report the error at the end of the file, not here"
    )


def test_the_catalogue_check_does_not_pipe_find_into_head():
    """The check was the bug, not the buildroot.

        find /usr/share/locale ... -name "*.mo" | head -1 | grep -q .

    `head -1` closes the pipe as soon as it has a line, `find` dies of
    SIGPIPE, and under `set -o pipefail` the pipeline reports 141 -- so the
    assertion fired while standing in a directory full of .mo files. Run
    32671483629 printed its own diagnostics proving them present:

        /usr/share/locale-langpack/en/LC_MESSAGES/coreutils.mo
        /usr/share/locale-langpack/en/LC_MESSAGES/dpkg.mo

    Those same diagnostics printed NOTHING for the dpkg configuration, which
    is why the path-exclude removal this test used to assert is gone: the
    image has no such directives, and the theory that it did was wrong.

    `-print -quit` stops find itself after the first hit, with no pipe and
    nothing to kill.
    """
    text = chain_text()
    assert '-name "*.mo" -print -quit' in text
    assert '-name "*.mo" 2>/dev/null \\\n         | head' not in text


def test_the_buildroot_proves_it_has_translations_not_just_a_charmap():
    """Checking the charmap alone would pass on the broken buildroot.

    en_US.UTF-8 resolved fine in run 32665378407 -- `locale charmap` said
    UTF-8 -- and gnome-desktop still discarded it, because a locale with no
    message catalogue is not a locale as far as that code is concerned. The
    assertion has to look for the catalogues.
    """
    text = chain_text()
    assert "no translation catalogues" in text
    assert "*.mo" in text


def test_the_language_pack_is_reinstalled_not_merely_installed():
    """dpkg applies path-exclude at UNPACK time.

    A package already present on the image kept its files stripped, and
    `apt-get install` on something already installed is a no-op. Lifting the
    exclusions only affects packages unpacked afterwards, so anything that was
    already there has to be unpacked again.
    """
    text = chain_text()
    assert "--reinstall" in text
    assert "language-pack-en-base" in text


def test_the_missing_catalogue_failure_prints_the_evidence():
    """The rule that has actually worked today.

    Three guesses at gnome-desktop:languages were wrong while the log showed
    a summary line; the fix came from making it print the test output. A
    buildroot assertion that says only "no catalogues" repeats that mistake
    one level down, and each round trip costs a run.
    """
    text = chain_text()
    failure = text.index("the buildroot has no translation catalogues")
    tail = text[failure:failure + 1200]
    assert "dpkg configuration" in tail
    assert "cat /etc/dpkg/dpkg.cfg" in tail
    assert "dpkg -L language-pack-en-base" in tail


def test_no_find_is_piped_into_head_anywhere_in_the_chain():
    """The class, not the instance.

    This script runs under `set -euo pipefail`. Any `find ... | head -N`
    reports SIGPIPE once find produces more than N lines, and pipefail turns
    that into a failed pipeline -- which `set -e` then turns into an aborted
    run. It stays invisible while output is short, so it ships green and
    fires later on a bigger input.

    Two of these existed at once: the catalogue assertion (fired immediately)
    and the testlog collector (latent until four logs exist, which is exactly
    when a package has failed and the logs matter). A pipeline ending in
    `|| true` is fine -- that absorbs the status deliberately.
    """
    offenders = []
    for line_number, line in enumerate(chain_text().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"find[^|]*\|\s*head\b", stripped) and "|| true" not in stripped:
            offenders.append(f"{line_number}: {stripped}")
    assert not offenders, (
        "find piped into head dies of SIGPIPE under pipefail; use -print -quit "
        "or collect to a file first:\n" + "\n".join(offenders)
    )


def test_translations_are_presented_where_gnome_desktop_looks_for_them():
    """Read out of gnome-desktop 51~alpha, not guessed.

    libgnome-desktop/gnome-languages.c, collect_locales(), warns only when
    BOTH collectors fail:

      collect_locales_from_localebin() runs `locale -a` and keeps a locale
        only if add_locale() accepts it. add_locale rejects any locale with
        no .mo under GNOMELOCALEDIR/<code>/LC_MESSAGES -- trying the full
        name, the id, and the bare language code in turn.
      collect_locales_from_directory() scandirs LIBLOCALEDIR for DIRECTORIES,
        and locale-gen writes a single locale-archive FILE, so it matches
        nothing.

    Both false -> the fatal warning -> the test aborts. So the locales were
    never the problem, and `language-pack-en` was the wrong package: Ubuntu
    puts catalogues in /usr/share/locale-langpack, which gettext finds via a
    distro patch and GNOMELOCALEDIR does not name.
    """
    text = chain_text()
    assert "/usr/share/locale-langpack/*/" in text
    assert "/usr/share/locale/$lang/LC_MESSAGES" in text


def test_the_buildroot_reports_the_predicate_it_is_trying_to_satisfy():
    """A count, every run, pass or fail.

    Four guesses at gnome-desktop:languages were wrong while the log showed a
    summary line and nothing else. The condition is decidable in one loop, so
    the buildroot states it rather than leaving the next reader to infer it
    from a SIGABRT an hour later.
    """
    text = chain_text()
    assert "locales that gnome-desktop would accept" in text
    # Reported unconditionally -- not inside the failure branch.
    report = text.index("locales that gnome-desktop would accept")
    failure = text.index("the buildroot has no translation catalogues")
    assert report < failure, "the count must print before, and independently of, any failure"


# ── The requirement debian/control never declared ───────────────────────────
#
# gnome-control-center 1:51~beta-1ubuntu1 build-depends on
# `libaccountsservice-dev (>= 23.11.69)`. resolute ships 23.13.9-8ubuntu5, so
# that constraint is SATISFIED and no edge enters the closure. Its meson.build
# wants `accountsservice >= 26.27.3`, and says so 1h47m into the chain.
#
# The engine is not wrong -- closing over declared Build-Depends is the rule
# that keeps the closure finite -- so the fix is two-part and both parts are
# tested here:
#
#   * the manifest NAMES accountsservice as a root, with the evidence, and the
#     measured order therefore places it before its consumer;
#   * the chain PRINTS which source to add the next time this happens, so the
#     class costs a log line instead of a two-hour chain.
#
# The classifier is a shell fragment, so it is tested by RUNNING it against
# the exact meson message from run 32678956022 with dpkg shimmed, not by
# matching its source text. A regex assertion over the script would pass just
# as happily if the block were unreachable or its sed expressions inverted --
# which is the specific way three earlier checks in this repository were wrong.

HINT_START = '        q=$(printf "\\047")'
MESON_MISMATCH = (
    "../meson.build:228:15: ERROR: Dependency lookup for accountsservice with "
    "method 'pkgconfig' failed: Invalid version, need 'accountsservice' "
    "['>= 26.27.3'] found '23.13.9'.\n"
)


def _hint_block() -> str:
    """The classifier, lifted out of the bash -lc body verbatim."""
    text = chain_text()
    start = text.index(HINT_START)
    end = text.index("\n        fi\n", start) + len("\n        fi\n")
    return text[start:end]


def _run_hint(tmp_path, build_log: str, *, pc_found: bool = True) -> str:
    """Run the classifier over `build_log` with find/dpkg/dpkg-query shimmed."""
    import subprocess

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "gnome-control-center.build.log").write_text(build_log, encoding="utf-8")

    binbin = tmp_path / "bin"
    binbin.mkdir()
    pc = "/usr/lib/x86_64-linux-gnu/pkgconfig/accountsservice.pc"
    (binbin / "find").write_text(
        "#!/bin/sh\n" + (f"echo {pc}\n" if pc_found else "exit 0\n"), encoding="utf-8"
    )
    # dpkg -S prints `pkg:arch: path`; the block cuts field 1 off that.
    (binbin / "dpkg").write_text(
        '#!/bin/sh\necho "libaccountsservice-dev:amd64: $2"\n', encoding="utf-8"
    )
    (binbin / "dpkg-query").write_text("#!/bin/sh\necho accountsservice\n", encoding="utf-8")
    for f in binbin.iterdir():
        f.chmod(0o755)

    script = tmp_path / "blk.sh"
    script.write_text(
        "set -uo pipefail\nsource=gnome-control-center\n"
        + _hint_block().replace("/work/logs", str(logs)),
        encoding="utf-8",
    )
    import os

    env = dict(os.environ, PATH=f"{binbin}:{os.environ['PATH']}")
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env
    ).stderr


def test_the_hint_names_the_source_package_to_add(tmp_path):
    """The whole point: turn the meson message into a manifest edit."""
    out = _run_hint(tmp_path, MESON_MISMATCH)
    assert "HINT: undeclared build-dependency version" in out
    # It must report BOTH numbers -- "needs a newer accountsservice" without
    # saying which version is present is the report that sent four earlier
    # investigations guessing.
    assert ">= 26.27.3" in out, out
    assert "23.13.9" in out, out
    # And the actionable half: the SOURCE name, not the pkgconfig module.
    assert "source package: accountsservice" in out, out
    assert "Add accountsservice to the roots list" in out, out


def test_the_hint_stays_silent_when_nothing_matches(tmp_path):
    """It must be able to NOT fire, or it says `accountsservice` about everything."""
    out = _run_hint(tmp_path, "dh_auto_build: error: make returned exit code 2\n")
    assert "HINT" not in out, out


def test_the_hint_admits_when_it_cannot_map_the_module(tmp_path):
    """No .pc on disk means no source name -- say so rather than invent one."""
    out = _run_hint(tmp_path, MESON_MISMATCH, pc_found=False)
    assert "HINT: undeclared build-dependency version" in out, out
    assert "Could not map accountsservice back to a source package" in out, out
    # It must not claim a source it did not resolve.
    assert "source package: " not in out, out


def test_the_classifier_runs_on_the_build_failure_path():
    """A block that is never reached explains nothing. It must sit inside the
    `FAILED build` branch, after the testlog surfacing and before `continue`."""
    text = chain_text()
    failed_build = text.index('echo "    FAILED build" >&2')
    hint = text.index(HINT_START)
    # `continue` ends the branch; the classifier must precede the one that
    # follows the failure.
    nxt = text.index('failed="$failed $source"', hint)
    assert failed_build < hint < nxt


def test_accountsservice_is_a_declared_root_with_its_evidence():
    """Named by hand, so the reason must be written down next to it."""
    manifest = (ROOT / "manifests" / "gnome51-deb.yaml").read_text(encoding="utf-8")
    assert "- accountsservice" in manifest
    assert "26.27.3" in manifest, "the version meson demands is the evidence"
    assert "32678956022" in manifest, "cite the run that measured it"


def test_the_measured_order_builds_accountsservice_before_its_consumer():
    """A root that lands in the same tier as gnome-control-center fixes nothing."""
    report = json.loads(
        (ROOT / "docs" / "gnome51-deb-gap.json").read_text(encoding="utf-8")
    )
    tiers = report["targets"]["ubuntu"]["tiers"]
    where = {pkg: i for i, tier in enumerate(tiers) for pkg in tier}
    assert "accountsservice" in where, "the root did not survive measurement"
    assert where["accountsservice"] < where["gnome-control-center"], where
