"""An interrupted createrepo_c must not poison the workspace forever.

createrepo_c stages into `.repodata/` and renames it to `repodata/` on
success. It refuses to start when that temp directory already exists:

    Temporary repodata directory .../.repodata/ already exists!
    (Another createrepo process is running?)

Nothing else is running. `update_local_repo` is called only from
`wait_one()`, a nested function the dispatch loop calls synchronously, so
there is never a second createrepo_c in the process. The directory is debris
from an INTERRUPTED run -- a cell that hit its deadline mid-index, a
cancelled shard, an OOM.

The recovery path made it permanent rather than transient: it removed
`repodata`, the FINISHED directory, and left `.repodata`, the one actually
blocking. So the fallback re-ran into the identical error, 15ms later, and
the chain exited 1 at `createrepo_c "${LOCAL_REPO}"`.

Measured, fanout run 33134251127:

    band      shards failed
    band0     6 of 16
    band1-x86 7 of 8
    total     14 of 30 completed

Every one of them died with `new RPMs: 0` -- they built nothing at all. The
served-NVR skip is what exposed it: a skip returns in milliseconds, so a
shard reaches its first metadata update almost immediately instead of
minutes into a real build.

The end-to-end assertion here is the one that matters: run the real
`update_local_repo` against a directory seeded with a stale `.repodata`, and
require it to succeed and produce valid metadata. It fails on the old code.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHAIN = ROOT / "scripts" / "build-chain.sh"


def test_the_retry_clears_the_temp_dir_not_just_the_finished_one():
    """`rm -rf repodata` alone leaves the blocker in place."""
    text = CHAIN.read_text(encoding="utf-8")
    assert 'rm -rf "${LOCAL_REPO}/.repodata" "${LOCAL_REPO}/repodata"' in text, (
        "the full re-index must clear .repodata; removing only repodata "
        "re-runs into 'Temporary repodata directory already exists'"
    )


def test_the_stale_dir_is_cleared_before_the_first_attempt_too():
    """Clearing it only on the retry still costs a failed --update and a
    scary WARNING on every shard that inherits debris."""
    text = CHAIN.read_text(encoding="utf-8")
    body = text.split("update_local_repo() {", 1)[1].split("\n}", 1)[0]
    first = body.index('rm -rf "${LOCAL_REPO}/.repodata"')
    update = body.index("createrepo_c --update")
    assert first < update, (
        "the stale temp dir must be gone before the first createrepo_c call"
    )


def test_why_no_lock_is_needed_is_written_down():
    """The error text blames a concurrent process, which is the obvious and
    WRONG reading; a future editor must not 'fix' this with flock."""
    body = CHAIN.read_text(encoding="utf-8")
    body = body.split("update_local_repo() {", 1)[1].split("\n}", 1)[0]
    assert "wait_one" in body and "synchronously" in body


def _stub_createrepo(bindir: Path) -> None:
    """A createrepo_c that reproduces the ONE behaviour under test.

    Modelled on the real tool: it stages into `.repodata/`, refuses to start
    when that directory already exists (the exact message the shards hit),
    and renames it to `repodata/` on success. Using a stub rather than
    skipping keeps this reproduction running in CI, where createrepo_c is not
    installed on the hosted runner that executes the test suite.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "createrepo_c"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "for a in \"$@\"; do case \"$a\" in --*) ;; *) repo=\"$a\" ;; esac; done\n"
        "tmp=\"$repo/.repodata\"\n"
        "if [ -d \"$tmp\" ]; then\n"
        "  echo \"Temporary repodata directory $tmp/ already exists!"
        " (Another createrepo process is running?)\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "mkdir -p \"$tmp\" && echo '<repomd/>' > \"$tmp/repomd.xml\"\n"
        "rm -rf \"$repo/repodata\" && mv \"$tmp\" \"$repo/repodata\"\n"
    )
    stub.chmod(0o755)


def _run_update_local_repo(repo: Path, bindir: Path) -> subprocess.CompletedProcess:
    """Drive the REAL update_local_repo body, scaffolding stubbed."""
    _stub_createrepo(bindir)
    func = CHAIN.read_text(encoding="utf-8")
    func = func.split("update_local_repo() {", 1)[1].split("\n}", 1)[0]
    script = (
        "set -euo pipefail\n"
        f'export PATH="{bindir}:$PATH"\n'
        f'LOCAL_REPO="{repo}"\n'
        'BACKEND="stub"\n'
        'ALMA_CANDIDATE=false\n'
        'log() { echo "==> $*"; }\n'
        'warn() { echo "WARNING: $*" >&2; }\n'
        "update_local_repo() {\n" + func + "\n}\n"
        "update_local_repo\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_update_local_repo_survives_a_stale_repodata_dir(tmp_path):
    """The reproduction: seed the debris, then require success.

    This is what the 14 shards actually hit, and it fails against the
    previous body of update_local_repo.
    """
    repo = tmp_path / "artifacts"
    repo.mkdir()
    stale = repo / ".repodata"
    stale.mkdir()
    (stale / "leftover.xml").write_text("<!-- interrupted mid-index -->")

    run = _run_update_local_repo(repo, tmp_path / "bin")
    assert run.returncode == 0, (
        "a stale .repodata must not be fatal\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    assert (repo / "repodata" / "repomd.xml").exists(), (
        "the repo must end up with real metadata, not just a clean exit"
    )
    assert not stale.exists(), "the debris must be gone, not merely worked around"


def test_the_clean_case_still_indexes(tmp_path):
    """The fix must not turn a healthy repo into a re-index every time."""
    repo = tmp_path / "artifacts"
    repo.mkdir()
    run = _run_update_local_repo(repo, tmp_path / "bin")
    assert run.returncode == 0, run.stderr
    assert (repo / "repodata" / "repomd.xml").exists()
    assert "attempting full re-index" not in run.stderr, (
        "a clean repo must go through --update, not the warning path"
    )
