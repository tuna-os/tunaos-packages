"""The action cache stores the cell's OUTPUT, never its build tree (#472).

Measured before the change, by downloading real cell artifacts from Package
Factory run 32489066325 (upload-artifact used the same path as the cache, so
the artifact composition WAS the cache composition):

    tideforge-cpptrace-devel-debian-amd64   70.9MB
        deb/         384 files   67,610,511   95.4%
        artifacts/     2 files    3,251,872    4.6%
      -- deb/source/cpptrace-1.0.4 alone was 60.6MB, and every .deb existed
         in three places: artifacts/, deb/artifacts/, deb/source/.

    tideforge-danksearch-el10-x86_64        13.9MB
        rpm/  50.5%   artifacts/  49.5%

The Actions cache is capped at 10GB per repository with LRU eviction, and the
largest cells in that run were 1.35GB, 916MB, 729MB. About ten entries
evicted everything, against ~294 planned cells.

Nothing on the hit path needs any of it: the result manifest is verified
against the expected key, verify-package-factory-cell.sh reads
`$out/artifacts` and nothing else, and both metadata files are regenerated
every run whether or not the cache hit.
"""
from __future__ import annotations

import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
CELL = ROOT / ".github" / "workflows" / "package-factory-cell.yml"
VERIFY = ROOT / "scripts" / "verify-package-factory-cell.sh"


def _steps() -> list[dict]:
    data = yaml.safe_load(CELL.read_text(encoding="utf-8"))
    (job,) = data["jobs"].values()
    return job["steps"]


def _cache_steps() -> list[dict]:
    return [s for s in _steps()
            if str(s.get("uses", "")).endswith("tideforge-action-cache")]


def _paths(step: dict) -> list[str]:
    return [line.strip() for line in step["with"]["path"].splitlines() if line.strip()]


def test_both_cache_steps_store_only_the_output():
    """The output, and nothing else — but "the output" is what VERIFY needs,
    not what I assumed it needed.

    This assertion originally listed just artifacts/ and action-result.json,
    on the claim that verify reads "$out/artifacts and nothing else". That is
    true for rpm and deb and false for Arch, whose branch passes
    $out/package-info.txt to validate-built-arch-package.py. Every Arch cache
    HIT then died on FileNotFoundError, latent only for as long as Arch cells
    kept missing.

    package-info.txt is a few hundred bytes of metadata, so it does not
    reopen what this test exists to prevent: the multi-hundred-MB build trees
    (deb/, rpm/, arch/), which the two tests below still ban outright.

    test_cache_carries_what_verify_reads.py derives this list from the verify
    script rather than restating it, and is the one that catches the next
    branch to read something new.
    """
    steps = _cache_steps()
    assert len(steps) == 2, "expected exactly a restore and a save"
    for step in steps:
        paths = _paths(step)
        assert paths == [
            ".factory/${{ matrix.id }}/artifacts",
            ".factory/${{ matrix.id }}/action-result.json",
            ".factory/${{ matrix.id }}/package-info.txt",
        ], step.get("id") or step.get("name")


def test_the_cache_stays_small():
    """The point of #472 was size. Guard it by kind: only the artifacts
    directory may be a directory; everything else must be a named file, so a
    future addition cannot quietly drag a tree back in."""
    for step in _cache_steps():
        for path in _paths(step):
            if path.endswith("/artifacts"):
                continue
            assert "." in path.rsplit("/", 1)[-1], path


def test_the_cache_never_names_a_build_tree():
    """deb/, rpm/ and arch/ are where the bulk lived. Naming the cell
    directory pulls them all in implicitly, so that is banned too."""
    for step in _cache_steps():
        for path in _paths(step):
            assert not re.search(r"/(deb|rpm|arch)\b", path), path
            assert not path.endswith("${{ matrix.id }}"), (
                "caching the cell directory drags the build tree with it"
            )


def test_restore_and_save_agree():
    """A save that stores less than restore expects is a permanent miss; a
    save that stores more is the bug this fixed."""
    restore, save = _cache_steps()
    assert _paths(restore) == _paths(save)


def test_the_verify_step_reads_only_what_is_cached():
    """The reason the trimmed set is sufficient, asserted against the script
    rather than trusted."""
    text = VERIFY.read_text()
    assert 'artifacts="$out/artifacts"' in text


def test_the_metadata_the_cache_drops_is_regenerated_every_run():
    """SHA256SUMS and the SPDX SBOM are not restored, so they must not be
    gated on a miss — otherwise a hit would upload and attest nothing."""
    for name in ("Write immutable artifact checksums", "Generate SPDX SBOM"):
        step = next(s for s in _steps() if s.get("name") == name)
        assert "if" not in step, f"{name} must run on a cache hit too"


# ------------------------------------------------------------ artifact upload


def _uploads() -> list[dict]:
    return [s for s in _steps() if str(s.get("uses", "")).startswith("actions/upload-artifact")]


def test_success_uploads_the_output_and_failure_uploads_the_build_tree():
    uploads = _uploads()
    assert len(uploads) == 2
    by_condition = {str(s.get("if")): s for s in uploads}
    assert set(by_condition) == {"success()", "failure()"}

    ok = [line.strip() for line in by_condition["success()"]["with"]["path"].splitlines()
          if line.strip()]
    assert ok == [
        ".factory/${{ matrix.id }}/artifacts/",
        ".factory/${{ matrix.id }}/metadata/",
        ".factory/${{ matrix.id }}/action-result.json",
    ]
    # On failure the build tree is exactly what a person debugging wants.
    assert by_condition["failure()"]["with"]["path"].strip() == ".factory/${{ matrix.id }}/"


def test_the_failure_upload_cannot_mask_the_real_failure():
    """A cell that died before creating its directory would otherwise fail
    HERE, replacing the actual error with an upload error."""
    failure = next(s for s in _uploads() if str(s.get("if")) == "failure()")
    assert failure["with"]["if-no-files-found"] == "warn"
    success = next(s for s in _uploads() if str(s.get("if")) == "success()")
    assert success["with"]["if-no-files-found"] == "error"


def test_both_uploads_keep_the_same_artifact_name_and_root():
    """Consumers see one artifact per cell either way, rooted at the cell
    directory, so the layout does not change with the outcome."""
    for step in _uploads():
        assert step["with"]["name"] == "${{ matrix.id }}"
        for line in step["with"]["path"].splitlines():
            if line.strip():
                assert line.strip().startswith(".factory/${{ matrix.id }}/")


# -------------------------------------------------------------- instrumenting


def test_every_cell_records_whether_the_cache_hit():
    """Before this, nothing in a run's output said whether the cache worked,
    which is why its value was an inference rather than a measurement."""
    verdict = next(s for s in _steps() if s.get("id") == "verdict")
    run = verdict["run"]
    assert "::notice title=action-cache::" in run
    assert "GITHUB_STEP_SUMMARY" in run
    assert "verdict=miss" in run and "verdict=hit" in run
