"""The mock dynamic-BuildRequires retry in scripts/build-chain.sh.

mock 6.7 + dnf5 5.4.2.1: mock's dynamic-BuildRequires loop (backend.py
rebuild_package -> installSrpmDeps -> pkg_manager.builddep) runs dnf5 builddep
on the generated .buildreqs.nosrc.rpm.  When %generate_buildrequires emits
requirements the base buildroot ALREADY satisfies, dnf5 fails the whole
transaction with `Failed to resolve the transaction: Package "<nevra>" is
already installed.` and mock raises BuildError -- with nothing actually wrong
with the package.

Measured across all five parallel desktop runs: niri-00 (31231968581) 70
occurrences over 11 distinct packages, kde-00 (31215339645) 31, gnome-00
(31215535607) 25, xfce (31215533814) 4 -- and no other error shape in three of
the four (kde's zimg has a genuine stale patch).  36 of the 37 "package
failures" were one toolchain bug.

The retry cannot be exercised for real without privileged mock, so these drive
the extracted control flow with the container call stubbed.  That is the part
a mistake would break: retrying unconditionally would mask real dependency
errors, and not retrying at all would leave the bug in place.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lib" / "build-chain" / "podman.sh"


def retry_block() -> str:
    """The `if ! _run_mock_container ...` guard, lifted verbatim."""
    text = SCRIPT.read_text()
    match = re.search(r'^    if ! _run_mock_container "".*?^    fi$', text, re.S | re.M)
    assert match, "retry guard not found in build-chain.sh"
    return match.group(0)


def run_case(tmp_path: Path, root_log: str | None, first_call_fails: bool) -> tuple[int, list[str]]:
    """Execute the real guard with a stubbed container call.

    Returns (exit status, the argument each _run_mock_container call received).
    """
    results = tmp_path / "results"
    results.mkdir(parents=True, exist_ok=True)
    if root_log is not None:
        (results / "root.log").write_text(root_log)
    calls = tmp_path / "calls"

    fail_logic = (
        'if [ "$(cat "$CALLS" | wc -l)" -le 1 ]; then return 1; fi; return 0'
        if first_call_fails
        else "return 0"
    )
    script = f"""
set -uo pipefail
builddir={tmp_path!s}
pkg_name=demo
CALLS={calls!s}
: > "$CALLS"
_run_mock_container() {{
    printf '%s|%s\\n' "${{1-<none>}}" "${{2-<none>}}" >> "$CALLS"
    {fail_logic}
}}
run_guard() {{
{retry_block()}
}}
run_guard
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return proc.returncode, recorded


ALREADY = 'Failed to resolve the transaction:\nPackage "less-704-3.hum1.x86_64" is already installed.\n'
OTHER = "No match for argument: totally-absent-package\nError: Unable to find a match\n"


def test_success_first_try_never_retries(tmp_path):
    status, calls = run_case(tmp_path, root_log=None, first_call_fails=False)
    assert status == 0
    assert len(calls) == 1, f"expected a single invocation, got {calls}"


def test_already_installed_failure_retries_once_with_the_override(tmp_path):
    status, calls = run_case(tmp_path, root_log=ALREADY, first_call_fails=True)
    assert status == 0, "the retry should carry the build to success"
    assert len(calls) == 2, f"expected exactly one retry, got {calls}"
    assert calls[0] == "|<none>", "the first attempt must be unmodified"
    assert calls[1] == "--config-opts=dynamic_buildrequires=False|clean", (
        "the retry asks for a clean-before-build chroot by name; passing mock's "
        "--clean COMMAND instead only cleans and never rebuilds (run 33753245495)"
    )


def test_unrelated_failure_does_not_retry(tmp_path):
    """A real dependency error must still fail loud on the first attempt."""
    status, calls = run_case(tmp_path, root_log=OTHER, first_call_fails=True)
    assert status != 0
    assert len(calls) == 1, f"a non-matching failure must not retry, got {calls}"


def test_missing_root_log_does_not_retry(tmp_path):
    """No log to match against is not licence to retry blindly."""
    status, calls = run_case(tmp_path, root_log=None, first_call_fails=True)
    assert status != 0
    assert len(calls) == 1


def test_retry_is_bounded_to_one_attempt(tmp_path):
    """If the override does not help, the build fails rather than looping."""
    tmp = tmp_path
    (tmp / "results").mkdir(parents=True, exist_ok=True)
    (tmp / "results" / "root.log").write_text(ALREADY)
    calls = tmp / "calls"
    script = f"""
set -uo pipefail
builddir={tmp!s}
pkg_name=demo
CALLS={calls!s}
: > "$CALLS"
_run_mock_container() {{ printf '%s|%s\\n' "${{1-<none>}}" "${{2-<none>}}" >> "$CALLS"; return 1; }}
run_guard() {{
{retry_block()}
}}
run_guard
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    recorded = calls.read_text().splitlines()
    assert proc.returncode != 0
    assert len(recorded) == 2, f"must not loop past one retry, got {recorded}"


def test_the_container_call_is_a_single_definition(tmp_path):
    """The retry must re-run the same invocation, not a drifting copy."""
    text = SCRIPT.read_text()
    assert text.count("_run_mock_container() {") == 1
    assert "${mock_extra_args}" in text, "the hook the retry rides on is gone"


INFORMATIONAL = (
    'DEBUG util.py:537:  Package gettext-0.22.5-6.el10.x86_64 is already installed.\n'
    'DEBUG util.py:537:  Package python3-devel-3.12.14-1.el10.x86_64 is already installed.\n'
)


def test_dnf5_informational_already_installed_lines_do_not_retry(tmp_path):
    """dnf5 prints "Package <nevra> is already installed." for every satisfied
    BuildRequires on a HEALTHY build. Matching those lines turned a genuine
    %files failure (input-remapper, run 33753245495) into a mislabelled
    "dnf5 bug" retry that hid the real error. Only the transaction failure
    is the bug."""
    status, calls = run_case(tmp_path, root_log=INFORMATIONAL, first_call_fails=True)
    assert status != 0
    assert len(calls) == 1, f"informational lines must not trigger the retry, got {calls}"


def test_clean_means_mock_cleans_before_the_build_not_the_clean_command():
    """mock takes one command per invocation and the last wins: `--rebuild
    ... --clean` runs `clean` and nothing else. A clean-before-build is
    mock's DEFAULT, so the retry must drop --no-clean rather than add
    --clean."""
    text = SCRIPT.read_text()
    fn = text[text.index("_run_mock_container() {"):]
    fn = fn[: fn.index("timeout --kill-after")]
    assert 'local mock_clean_flag="--no-clean"' in fn
    assert '[ "${2:-}" = "clean" ] && mock_clean_flag=""' in fn
    assert '"--clean"' not in text, "nothing may pass mock's --clean command alongside --rebuild"
