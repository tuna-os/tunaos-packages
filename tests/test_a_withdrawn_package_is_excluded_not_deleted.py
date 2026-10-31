"""A withdrawn package is excluded from repodata, never deleted (#525).

Issue #525: a served build older than upstream's shadows upstream's fix,
because this factory's desktop manifests add our repository at dnf
`priority: 5` -- absolute, not a tie-break. The obvious remedy -- delete the
stale RPM from the live bucket -- is exactly the shape of mistake #124 /
INCIDENT-repo-wipe-gnome exists to prevent, and publish-rpm-wave.sh's NEVER
SHRINK guard would refuse a tree that came out smaller than it went in.

createrepo_c's own `-x PATTERN` sidesteps the guard rather than crossing it:
the file stays on disk (count_rpms still counts it, baseline == final, the
guard never fires) and is merely left out of the repodata createrepo_c
regenerates. scripts/plan-withdrawn-excludes.py turns
manifests/withdrawn-packages.yaml into those patterns;
scripts/publish-rpm-wave.sh's --exclude-file turns them into createrepo_c
`-x` arguments. This file is the contract between the two: a pattern in the
file must reach createrepo_c's argv, comments and blank lines must not, and
the excluded file must still be sitting right there afterward.

rpmsign, createrepo_c and gpg are stubbed -- the contract under test is
which argv createrepo_c receives and which files survive on disk, not real
RPM signing or repository indexing.
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WAVE = REPO / "scripts" / "publish-rpm-wave.sh"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(tmp_path: Path, extra_args: list[str]) -> tuple[subprocess.CompletedProcess, Path, Path, Path]:
    """Set up a staged wave + a stale already-served package, run the wave
    publisher with stubbed binaries, return (result, repo, createrepo_log,
    stale_rpm)."""
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "newpkg-1.0-1.fc43.x86_64.rpm").write_text("new rpm bytes")

    repo = tmp_path / "repo"
    repo.mkdir()
    stale_rpm = repo / "sudo-1.9.17-1.p2.fc43.x86_64.rpm"
    stale_rpm.write_text("stale rpm bytes, already published")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "rpmsign", "exit 0")
    createrepo_log = tmp_path / "createrepo-args.log"
    # Logs its own argv, then creates repodata/repomd.xml so sign-repomd.sh
    # (run for real afterward) has something to sign.
    _stub(bin_dir, "createrepo_c", f'''
echo "$@" >> "{createrepo_log}"
last="${{@: -1}}"
mkdir -p "$last/repodata"
echo "<repomd/>" > "$last/repodata/repomd.xml"
''')
    _stub(bin_dir, "gpg", '''
out=""
while [ $# -gt 0 ]; do
  case "$1" in --output) out="$2"; shift 2 ;; *) shift ;; esac
done
echo "SIGNATURE" > "$out"
''')

    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    result = subprocess.run(
        ["bash", str(WAVE), "--staged", str(staged), "--repo", str(repo),
         "--subdir", "build-chain", *extra_args],
        capture_output=True, text=True, env=env,
    )
    return result, repo, createrepo_log, stale_rpm


def test_a_listed_pattern_reaches_createrepo_c(tmp_path) -> None:
    exclude_file = tmp_path / "excludes.txt"
    exclude_file.write_text(
        "# withdrawn: public-hummingbird ships 1.9.17-16.p2.hum1 (#525)\n"
        "*sudo-1.9.17-1.p2.fc43.*.rpm\n"
        "\n"
    )

    result, repo, createrepo_log, stale_rpm = _run(
        tmp_path, ["--exclude-file", str(exclude_file)])

    assert result.returncode == 0, result.stderr
    args = createrepo_log.read_text().splitlines()
    assert len(args) == 1, "createrepo_c must run exactly once (--update succeeds)"
    assert "-x *sudo-1.9.17-1.p2.fc43.*.rpm" in args[0], (
        "the pattern in --exclude-file did not reach createrepo_c's argv: "
        f"{args[0]!r}"
    )
    # The comment and the blank line must not become patterns of their own.
    assert args[0].count("-x") == 1


def test_the_excluded_file_is_not_deleted(tmp_path) -> None:
    """Withdrawal is an index-only change. The never-shrink guard must see
    the same file count before and after, or a real withdrawal would trip
    it (#124 / INCIDENT-repo-wipe-gnome)."""
    exclude_file = tmp_path / "excludes.txt"
    exclude_file.write_text("*sudo-1.9.17-1.p2.fc43.*.rpm\n")

    result, repo, _createrepo_log, stale_rpm = _run(
        tmp_path, ["--exclude-file", str(exclude_file)])

    assert result.returncode == 0, result.stderr
    assert stale_rpm.is_file(), (
        "excluding a package from repodata must not remove it from disk -- "
        "that is what makes the withdrawal reversible"
    )
    assert stale_rpm.read_text() == "stale rpm bytes, already published", (
        "the excluded file's bytes must be untouched"
    )


def test_no_exclude_file_means_no_dash_x(tmp_path) -> None:
    """Every existing caller omits --exclude-file; behaviour must not move."""
    result, repo, createrepo_log, _stale_rpm = _run(tmp_path, [])

    assert result.returncode == 0, result.stderr
    args = createrepo_log.read_text().splitlines()
    assert len(args) == 1
    assert "-x" not in args[0]


def test_an_empty_or_missing_exclude_file_is_a_no_op(tmp_path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing withdrawn yet\n\n")
    missing = tmp_path / "does-not-exist.txt"

    for candidate, label in ((empty, "empty"), (missing, "missing")):
        case_dir = tmp_path / label
        case_dir.mkdir()
        result, repo, createrepo_log, _stale_rpm = _run(
            case_dir, ["--exclude-file", str(candidate)])
        assert result.returncode == 0, result.stderr
        args = createrepo_log.read_text().splitlines()
        assert "-x" not in args[0], f"{label} exclude file produced a -x arg"
