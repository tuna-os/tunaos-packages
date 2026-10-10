"""The spectool download retry in scripts/build-chain.sh and scripts/lib/build-chain/native.sh.

build_package() fetches upstream source tarballs using spectool. The chain
pulls from several hundred upstream hosts, many of them personal or small
project servers. A leg can run for hours; a single transient blip (such as
[Errno 101] Network is unreachable in run 33840428161) ends the package and
every downstream package that BuildRequires it.

What a mistake here would break:
- Retrying unconditionally on 404 or unresolvable hosts wastes minutes per
  broken spec and hides missing upstream sources behind slow timeouts.
- Not retrying on transient connection failures (unreachable network,
  connection refused, connection reset, timeouts, 502/503/504) kills long-running
  build chains over brief network weather.
- Not bounding retries would loop endlessly.

These unit tests verify:
1. Happy path: succeeds on first try with 1 invocation.
2. Transient connection error: retried with backoff until it succeeds.
3. Persistent transient connection error: fails after bounded attempts and
   distinguishes connection error in the message.
4. Permanent 404 error: fails immediately with 1 invocation without retrying,
   noting the spec URL needs updating.
5. Permanent host name resolution error: fails immediately with 1 invocation
   without retrying, noting the host name does not resolve.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_CHAIN = ROOT / "scripts" / "build-chain.sh"
NATIVE = ROOT / "scripts" / "lib" / "build-chain" / "native.sh"


def extract_build_chain_spectool_block() -> str:
    """Extract the spectool retry loop from scripts/build-chain.sh."""
    text = BUILD_CHAIN.read_text()
    match = re.search(
        r'(local spectool_attempts=.*?^    done)',
        text,
        re.S | re.M,
    )
    assert match, "spectool retry block not found in build-chain.sh"
    return match.group(1)


def extract_native_spectool_block() -> str:
    """Extract the spectool retry loop from scripts/lib/build-chain/native.sh."""
    text = NATIVE.read_text()
    match = re.search(
        r'(local spectool_attempts=.*?^    done)',
        text,
        re.S | re.M,
    )
    assert match, "spectool retry block not found in native.sh"
    return match.group(1)


def run_spectool_case(
    tmp_path: Path,
    outputs: list[tuple[int, str]],
    *,
    attempts: int = 4,
    delay: int = 0,
) -> tuple[int, list[str], str, str]:
    """Execute the build-chain.sh spectool retry guard with a stubbed _run_spectool.

    outputs is a list of (returncode, stdout_stderr_string) for each call.
    Returns (exit_code, calls_log, stdout, stderr).
    """
    calls = tmp_path / "calls"
    output_scripts = []
    for idx, (code, out_str) in enumerate(outputs, 1):
        output_file = tmp_path / f"out_{idx}.txt"
        output_file.write_text(out_str)
        output_scripts.append(f"""
        if [ "$count" -eq {idx} ]; then
            cat "{output_file}"
            return {code}
        fi
""")

    dispatch = "\n".join(output_scripts)

    script = f"""
set -uo pipefail
builddir="{tmp_path}"
pkg_name="malcontent"
CALLS="{calls}"
: > "$CALLS"

_run_spectool() {{
    printf 'call\\n' >> "$CALLS"
    local count
    count=$(wc -l < "$CALLS")
{dispatch}
    return 0
}}

export SPECTOOL_ATTEMPTS={attempts}
export SPECTOOL_DELAY={delay}

run_guard() {{
{extract_build_chain_spectool_block()}
}}

run_guard
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return proc.returncode, recorded, proc.stdout, proc.stderr


NETWORK_UNREACHABLE = """
HTTPSConnectionPool(host='tecnocode.co.uk', port=443): Max retries exceeded with url:
  /downloads/malcontent/malcontent-0.14.0.tar.xz
  (Caused by NewConnectionError(... [Errno 101] Network is unreachable))
"""

CONNECTION_REFUSED = """
requests.exceptions.ConnectionError: ('Connection aborted.', ConnectionRefusedError(111, 'Connection refused'))
"""

HTTP_404 = """
requests.exceptions.HTTPError: 404 Client Error: Not Found for url: https://tecnocode.co.uk/downloads/malcontent/malcontent-0.14.0.tar.xz
"""

DNS_FAILURE = """
urllib3.exceptions.MaxRetryError: HTTPSConnectionPool(host='nonexistent.badhost.example.invalid', port=443):
  Max retries exceeded with url: /foo.tar.xz
  (Caused by NameResolutionError("<...>: Failed to resolve 'nonexistent.badhost.example.invalid' ([Errno -2] Name or service not known)"))
"""


def test_success_on_first_attempt_does_not_retry(tmp_path):
    rc, calls, stdout, stderr = run_spectool_case(
        tmp_path,
        [(0, "Getting https://example.com/tarball.tar.xz\n")],
    )
    assert rc == 0
    assert len(calls) == 1
    assert "Getting https://example.com/tarball.tar.xz" in stdout
    assert "retrying in" not in stderr


def test_transient_connection_error_retries_and_succeeds(tmp_path):
    """Run 33840428161: Network is unreachable on attempt 1, resolves on attempt 2."""
    rc, calls, stdout, stderr = run_spectool_case(
        tmp_path,
        [
            (1, NETWORK_UNREACHABLE),
            (0, "Getting https://tecnocode.co.uk/downloads/malcontent/malcontent-0.14.0.tar.xz\n"),
        ],
    )
    assert rc == 0
    assert len(calls) == 2
    assert "spectool download failed with connection error (attempt 1/4)" in stderr
    assert "spectool download succeeded on attempt 2" in stdout


def test_transient_connection_error_exhausts_retries(tmp_path):
    rc, calls, stdout, stderr = run_spectool_case(
        tmp_path,
        [
            (1, CONNECTION_REFUSED),
            (1, CONNECTION_REFUSED),
            (1, CONNECTION_REFUSED),
            (1, CONNECTION_REFUSED),
        ],
        attempts=4,
    )
    assert rc != 0
    assert len(calls) == 4
    assert "ERROR: spectool failed for malcontent after 4 attempts (connection error)" in stderr


def test_http_404_fails_immediately_without_retry(tmp_path):
    """A 404 Not Found is a dead link or spec update needed, not transient weather."""
    rc, calls, stdout, stderr = run_spectool_case(
        tmp_path,
        [(1, HTTP_404)],
        attempts=4,
    )
    assert rc != 0
    assert len(calls) == 1, f"404 must not be retried, got {len(calls)} calls"
    assert "ERROR: spectool failed for malcontent: source not found (HTTP 404); spec URL needs updating" in stderr


def test_dns_name_resolution_fails_immediately_without_retry(tmp_path):
    """An unresolvable host name is a broken spec URL, not a transient flake."""
    rc, calls, stdout, stderr = run_spectool_case(
        tmp_path,
        [(1, DNS_FAILURE)],
        attempts=4,
    )
    assert rc != 0
    assert len(calls) == 1, f"DNS failure must not be retried, got {len(calls)} calls"
    assert "ERROR: spectool failed for malcontent: host name does not resolve; spec URL needs updating" in stderr


def test_native_spectool_retry_distinguishes_errors(tmp_path):
    """Verify scripts/lib/build-chain/native.sh carries matching retry logic for 404."""
    calls = tmp_path / "calls"
    output_file = tmp_path / "out_404.txt"
    output_file.write_text(HTTP_404)

    script = f"""
set -uo pipefail
builddir="{tmp_path}"
pkg_name="demo"
spec_basename="demo.spec"
spectool_dest="{tmp_path}/SOURCES"
mkdir -p "$spectool_dest"
CALLS="{calls}"
: > "$CALLS"

spectool() {{
    printf 'call\\n' >> "$CALLS"
    cat "{output_file}"
    return 1
}}

err() {{
    echo "ERROR: $*" >&2
}}

log() {{
    echo "LOG: $*"
}}

export SPECTOOL_ATTEMPTS=4
export SPECTOOL_DELAY=0

run_guard() {{
{extract_native_spectool_block()}
}}

run_guard
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    recorded = calls.read_text().splitlines() if calls.exists() else []
    assert proc.returncode != 0
    assert len(recorded) == 1
    assert "source not found (HTTP 404)" in proc.stderr


def test_native_spectool_retry_transient_success(tmp_path):
    """Verify scripts/lib/build-chain/native.sh retries transient connection errors."""
    calls = tmp_path / "calls"
    err_file = tmp_path / "out_err.txt"
    err_file.write_text(NETWORK_UNREACHABLE)

    script = f"""
set -uo pipefail
builddir="{tmp_path}"
pkg_name="demo"
spec_basename="demo.spec"
spectool_dest="{tmp_path}/SOURCES"
mkdir -p "$spectool_dest"
CALLS="{calls}"
: > "$CALLS"

spectool() {{
    printf 'call\\n' >> "$CALLS"
    local count
    count=$(wc -l < "$CALLS")
    if [ "$count" -eq 1 ]; then
        cat "{err_file}"
        return 1
    fi
    echo "Getting tarball.tar.xz"
    return 0
}}

err() {{
    echo "ERROR: $*" >&2
}}

log() {{
    echo "LOG: $*"
}}

export SPECTOOL_ATTEMPTS=4
export SPECTOOL_DELAY=0

run_guard() {{
{extract_native_spectool_block()}
}}

run_guard
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    recorded = calls.read_text().splitlines() if calls.exists() else []
    assert proc.returncode == 0
    assert len(recorded) == 2
    assert "spectool download succeeded on attempt 2" in proc.stdout

