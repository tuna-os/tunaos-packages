"""One level of tooling support per declared format — enforced, not hoped.

The factory ships desktops for RPM, DEB, and pacman targets as equals
(RFC 011); its checks and records must be equals too, or the tooling
quietly becomes an EL-focused factory with some extra targets attached.
This ledger asserts, per format the contract declares, that the
capabilities exist and are wired. Adding a format — or a capability —
without its counterparts turns this file red, which is the review
conversation happening in CI instead of in an incident.

Known, deliberate asymmetries are pinned as such below, with the reason
they are asymmetric, so they read as decisions rather than drift.
"""
from __future__ import annotations

import importlib.util
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, filename=None):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / (filename or f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contract_formats() -> set[str]:
    contract = yaml.safe_load(
        (ROOT / "manifests" / "package-factory.yaml").read_text())
    return {t.get("format") for t in contract["targets"].values()}


def test_every_declared_format_is_known_to_the_index_layer():
    repo_index = load("repo_index")
    missing = contract_formats() - set(repo_index.FORMATS)
    assert not missing, (
        f"formats {missing} are declared in manifests/package-factory.yaml "
        "but scripts/repo_index.py cannot read them — add a reader and a "
        "comparator before adding the target")


def test_every_format_has_its_own_version_comparator():
    """Never one format's ruler for another's versions.

    The comparators provably disagree on real versions (deb vs rpm on
    `1.0.1`/`1.0+1`; pacman vs rpm on `1.0a`/`1.0`), so sharing one
    would give wrong answers somewhere, silently.
    """
    repo_index = load("repo_index")
    modules = {fmt: repo_index.version_module(fmt).__name__
               for fmt in repo_index.FORMATS}
    assert modules == {"rpm": "rpm_vercmp", "deb": "deb_version",
                       "pkg.tar.zst": "pacman_db", "gpkg": "gentoo_binhost"}
    assert len(set(modules.values())) == len(modules), (
        "two formats share a comparator; their version orderings differ")


def test_every_format_has_a_constraint_check():
    repo_index = load("repo_index")
    for fmt in repo_index.FORMATS:
        assert hasattr(repo_index.version_module(fmt), "satisfies"), fmt


def test_every_publish_path_gates_reverse_dependencies():
    """rpm gates in the shared wave script; deb and arch regenerate
    their index in place, so they gate old-vs-new in their publishers."""
    assert "check-reverse-deps.py" in (
        ROOT / "scripts" / "publish-rpm-wave.sh").read_text()
    assert "check-index-regression.py --format deb" in (
        ROOT / ".github" / "workflows" / "publish-tideforge-debs.yml"
    ).read_text()
    assert "check-index-regression.py --format pacman" in (
        ROOT / ".github" / "workflows" / "publish-tideforge-arch.yml"
    ).read_text()
    assert "check-index-regression.py --format gpkg" in (
        ROOT / ".github" / "workflows" / "publish-tideforge-gentoo.yml"
    ).read_text()


def test_every_chain_records_a_diffable_buildroot():
    """rpm from mock's own logs, deb from dpkg-query; one differ reads
    both conventions (tests/test_buildroot_manifests.py holds the
    per-format parsing pins)."""
    assert "record_buildroot_manifest" in "\n".join(
        (ROOT / path).read_text()
        for path in (
            "scripts/build-chain.sh",
            "scripts/lib/build-chain/podman.sh",
            "scripts/lib/build-chain/mock.sh",
        )
    )
    assert "buildroot.txt" in (
        ROOT / "scripts" / "build-deb-chain.sh").read_text()


def test_hygiene_is_driven_by_the_contract_not_a_format_filter():
    """The hygiene tool must not skip targets by format. Its per-format
    readers come from repo_index; the only legitimate skip is a target
    with no served index to read."""
    body = (ROOT / "scripts" / "check-published-hygiene.py").read_text()
    assert 'format") != "rpm"' not in body
    assert "iter_rows(url, fmt" in body


def test_the_deliberate_asymmetries_are_documented_where_they_live():
    """Gaps that are decisions, kept visible:

    * file-conflict data exists only in rpm-md — flat APT and pacman
      .db carry no file lists, so the hygiene tool says so in its scope
      notes rather than reporting false cleanliness.
    * per-package build timers exist only in mock's output — the deb
      chain logs no equivalent, so the throughput tool documents its
      rpm-only reach.
    """
    hygiene = (ROOT / "scripts" / "check-published-hygiene.py").read_text()
    assert "not cleanliness" in hygiene
    throughput = (ROOT / "scripts" / "collect-cell-throughput.py").read_text()
    assert "mock" in throughput
