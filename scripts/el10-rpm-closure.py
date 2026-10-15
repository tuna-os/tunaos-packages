#!/usr/bin/env python3
"""Scaffolding ONLY: EL10 receipt/closure draft checks. Not native closure.

Candidate-only scaffolding: validates ``stage-tuna-desktop-el10.py
--only-el10`` receipt shapes, emits the provider/base ABI closure
measurement plan that CI runs later inside an existing job, and
reserves the ``admit_measured_closure`` entry point. Nothing here
admits, measures, or qualifies any native package: admission always
fails until a real measured producer, artifact binding, cryptographic
signature verification, and exhaustive per-require provider/ABI
observations are implemented. Pure offline checks plus a plan emitter;
no network, no package manager, no images, no publication, no pin
changes.

Every check fails closed on missing, partial, default-omitted, or
mistyped fields. ``validate_closure_report`` proves report shape and
nothing else. Inventing a measured EL10 green is forbidden.
"""

from __future__ import annotations

import json
import re

# Must equal the stager's EL10_ONLY_CELLS; tests bind them (importlib).
SKIPJACK_EL10_CELLS = frozenset(
    {("skipjack", "el10", arch) for arch in ("x86_64", "aarch64")}
)

# Must equal the stager's ARCH_PLATFORMS; tests bind them (importlib).
ARCH_PLATFORMS = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}

_HEX = frozenset("0123456789abcdef")
_RUN_URL = re.compile(
    r"https://github\.com/tuna-os/tunaos-packages/actions/runs/[0-9]+\Z"
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _is_hex(value, length):
    # type() is str first: no TypeError on None/int/list, and no
    # True == 1 style coercion anywhere in this module.
    return (
        type(value) is str
        and len(value) == length
        and all(char in _HEX for char in value)
    )


def _is_strict_int(value):
    return type(value) is int


def _is_nonempty_str(value):
    return type(value) is str and bool(value.strip())


def _cell_key(cell):
    return f"{cell['consumer']}-{cell['target']}-{cell['arch']}"


def _check_base_image(value, where):
    require(type(value) is str, f"{where} base_image must be a string")
    repo, sep, digest = value.partition("@sha256:")
    require(sep and repo and " " not in repo and "\t" not in repo, f"{where} base_image has no pinned repository")
    require(_is_hex(digest, 64), f"{where} base_image digest is not 64 hex")


def canonical_sha256(document):
    """Stable hash binding plans, receipts, and reports to exact bytes."""
    require(isinstance(document, dict), "only objects are hashable inputs")
    return __import__("hashlib").sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_candidate_receipt(receipt, expected_subset=("skipjack",)):
    """Accept a candidate-only subset receipt; return ``(source_sha, cells)``.

    Requires the COMPLETE expected cell set: a receipt staging one of
    two cells, a duplicate, a wrong-arch entry, or an unpinned base is
    rejected. Alma consumer cells must never be relabeled as EL10
    evidence.
    """
    require(isinstance(receipt, dict), "receipt must be an object")
    require(
        _is_strict_int(receipt.get("schema")) and receipt["schema"] == 1,
        "receipt schema must be the integer 1",
    )
    require(receipt.get("candidate_only") is True, "receipt is not candidate-only")
    require(receipt.get("release_qualified") is False, "receipt claims release qualification")
    require(receipt.get("published") is False, "receipt claims publication")
    require(_is_hex(receipt.get("source_sha"), 40), "exact source SHA required")
    for key in ("source_archive_sha256", "vendor_archive_sha256", "lock_sha256"):
        require(_is_hex(receipt.get(key), 64), f"bad {key}")
    subset = receipt.get("cell_subset")
    require(
        type(subset) is list and subset == list(expected_subset),
        f"receipt covers {subset!r}, want {list(expected_subset)!r}",
    )
    cells = receipt.get("cells")
    require(type(cells) is list and len(cells) > 0, "receipt stages no cells")
    expected = {triple for triple in SKIPJACK_EL10_CELLS if triple[0] in subset}
    require(expected, "subset names no known cells")
    seen = set()
    for cell in cells:
        require(isinstance(cell, dict), "cell must be an object")
        triple = (cell.get("consumer"), cell.get("target"), cell.get("arch"))
        require(triple in SKIPJACK_EL10_CELLS, f"cell outside EL10 subset: {triple!r}")
        require(triple not in seen, f"duplicate cell: {triple!r}")
        seen.add(triple)
        where = f"cell {triple[0]}/{triple[1]}/{triple[2]}"
        _check_base_image(cell.get("base_image"), where)
        require(_is_nonempty_str(cell.get("base_reference")), f"{where} base_reference missing")
        require(
            cell.get("platform") == ARCH_PLATFORMS[triple[2]],
            f"{where} platform {cell.get('platform')!r} does not match arch {triple[2]}",
        )
        require(_is_hex(cell.get("recipe_sha256"), 64), f"{where} has a bad recipe hash")
    require(
        seen == expected,
        f"incomplete cell set: missing {sorted(expected - seen)!r}",
    )
    return receipt["source_sha"], cells


def _select_index(published_index, arch, target):
    # Exactly this cell's arch: a dict selects its own entry only, never
    # a sibling arch's supply; a bare string is one index for all arches.
    if type(published_index) is str:
        require(bool(published_index.strip()), f"{target} declares an empty index")
        return [published_index]
    if isinstance(published_index, dict):
        require(arch in published_index, f"{target} declares no served index for {arch}")
        entry = published_index[arch]
        if type(entry) is str:
            require(bool(entry.strip()), f"{target}/{arch} index is empty")
            return [entry]
        require(
            type(entry) is list
            and entry
            and all(_is_nonempty_str(url) for url in entry),
            f"{target}/{arch} index must be a non-empty URL list",
        )
        return list(entry)
    raise ValueError(f"{target} published_index has an unusable shape")


def _check_requires(values, kind):
    require(type(values) is list and len(values) > 0, f"empty {kind} requires")
    for entry in values:
        require(_is_nonempty_str(entry), f"{kind} requires a non-empty string, got {entry!r}")
    require(len(set(values)) == len(values), f"{kind} requires list duplicate entries")


def _check_repo_list(values, kind):
    require(type(values) is list, f"{kind} repositories must be a list")
    for entry in values:
        require(_is_nonempty_str(entry), f"{kind} repository entry malformed: {entry!r}")
    return list(values)


def plan_base_closure(cell, target_cfg, build_requires, runtime_requires):
    """Plan the base/provider ABI closure measurement for one staged cell.

    Pure data: resolves the repository set from the production target
    definition (this arch's served index only, plus build/system
    repositories) and emits the ordered measurement steps CI executes.
    Fails here on a missing target definition, cross-arch supply, or a
    malformed require set.
    """
    require(isinstance(cell, dict), "cell must be an object")
    triple = (cell.get("consumer"), cell.get("target"), cell.get("arch"))
    require(triple in SKIPJACK_EL10_CELLS, f"cell outside EL10 subset: {triple!r}")
    require(isinstance(target_cfg, dict) and target_cfg, "missing native target definition")
    require(target_cfg.get("format") == "rpm", "closure planning needs an rpm target")
    require(triple[2] in target_cfg.get("architectures", []), "arch not native to target")
    _check_requires(build_requires, "build")
    _check_requires(runtime_requires, "runtime")
    key = _cell_key(cell)
    repos = {
        "published_index": _select_index(
            target_cfg.get("published_index"), triple[2], triple[1]
        ),
        "build": _check_repo_list(target_cfg.get("build_repositories", []), "build"),
        "system": _check_repo_list(target_cfg.get("system_repositories", []), "system"),
    }
    steps = [
        {
            "name": "resolve-build-requires",
            "repos": repos["published_index"],
            "packages": list(build_requires),
        },
        {
            "name": "resolve-runtime-requires",
            "repos": repos["published_index"],
            "packages": list(runtime_requires),
        },
        {
            "name": "abi-closure",
            "repos": repos["published_index"],
            "note": "fail on unavailable packages or incompatible ABIs",
        },
    ]
    return {
        "cell": key,
        "arch": triple[2],
        "base_image": cell.get("base_image"),
        "repos": repos,
        "requires": {"build": list(build_requires), "runtime": list(runtime_requires)},
        "steps": steps,
    }


def _check_producer(producer):
    require(isinstance(producer, dict), "producer must be an object")
    run_id = producer.get("run_id")
    require(_is_strict_int(run_id) and run_id > 0, "producer run_id must be a positive int")
    run_url = producer.get("run_url")
    require(type(run_url) is str and _RUN_URL.match(run_url) is not None, "producer run_url is not a tunaos-packages run")


def _check_signatures(signatures):
    require(isinstance(signatures, dict) and signatures, "signatures must be a non-empty object")
    require(_is_nonempty_str(signatures.get("signed_by")), "signatures lack an identity")
    require(_is_nonempty_str(signatures.get("key")), "signatures lack a key identity")


def validate_closure_report(report, cell_key):
    """Strict schema check for a measured closure report. DRAFT ONLY.

    Necessary but never sufficient: passing here proves shape, not
    closure. Only ``admit_measured_closure`` admits, by binding the
    report to the exact plan, receipt, source, and exhaustive evidence.
    """
    require(isinstance(report, dict), "report must be an object")
    require(report.get("cell") == cell_key, "report is for another cell")
    require(_is_hex(report.get("plan_sha256"), 64), "report lacks a plan binding")
    require(_is_hex(report.get("receipt_sha256"), 64), "report lacks a receipt binding")
    require(_is_hex(report.get("source_sha"), 40), "report lacks a source binding")
    require(_is_nonempty_str(report.get("base_image")), "report lacks a base image")
    _check_producer(report.get("producer"))
    _check_signatures(report.get("signatures"))
    dependencies = report.get("dependencies")
    require(isinstance(dependencies, dict) and dependencies, "report lacks dependency observations")
    for name, observed in dependencies.items():
        require(_is_nonempty_str(name), "nameless dependency entry")
        require(type(observed) is list and observed, f"dependency {name} has no observations")
    covers = report.get("covers_requires")
    require(isinstance(covers, dict), "report lacks a requires echo")
    for kind in ("build", "runtime"):
        echoed = covers.get(kind)
        require(type(echoed) is list and echoed, f"report echoes no {kind} requires")
        for entry in echoed:
            require(_is_nonempty_str(entry), f"report {kind} echo malformed: {entry!r}")
    missing = report.get("missing")
    require(type(missing) is list, "report missing field must be a list")
    require(not missing, f"unavailable packages: {missing!r}")
    conflicts = report.get("abi_conflicts")
    require(type(conflicts) is list, "report abi_conflicts field must be a list")
    require(not conflicts, f"incompatible ABIs: {conflicts!r}")
    packages = report.get("packages")
    require(type(packages) is list and packages, "report resolves no packages")
    for entry in packages:
        require(isinstance(entry, dict), "package entry must be an object")
        require(_is_nonempty_str(entry.get("name")), "nameless package entry")
        require(entry.get("status") == "resolved", f"unresolved package: {entry.get('name')!r}")
        require(_is_nonempty_str(entry.get("evr")), f"package {entry.get('name')} lacks an EVR")
    return len(packages)


def admit_measured_closure(report, plan, receipt):
    """Mandatory measured admission. NOT IMPLEMENTED: always fails.

    Kept as the single admission entry point with its final signature
    so no caller can mistake the draft schema check for admission.
    Real admission requires, at minimum: a measured producer whose
    run_id is bound to the run_url suffix and a trusted producer
    head; artifact binding to the producing run; cryptographic
    signature verification (not arbitrary strings); and exhaustive
    per-require provider/ABI observations covering the entire plan
    require sets. Until all of those are implemented and measured,
    every report is rejected — including self-consistent ones.
    """
    _ = (report, plan, receipt)
    raise ValueError(
        "measured admission not implemented: no report is admitted until "
        "producer, artifact, signature, and exhaustive per-require "
        "provider/ABI observations are measured"
    )
