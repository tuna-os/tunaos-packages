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

Measured admission lives in ``admit_producer_closure`` below: the
policy-bound successor that verifies a native producer report against
explicit trust roots (trusted heads, workflow name, signing keys,
served indexes). ``admit_measured_closure`` is unchanged and stays the
no-policy degenerate case: without trust roots there is nothing to
verify a report's claimed measurements against, so every report is
still rejected there, exactly as before.
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


# --- Measured producer admission (policy-bound) ---
#
# Every admitted claim needs an observed artifact plus a hash plus a
# signature/provenance binding. Anything less fails closed with the
# specific gap named. Trust roots come ONLY from `policy`, never from
# the report itself: heads, workflow name, signing key identities, and
# the served index set per arch.

# Container `uname -m` spelling each arch must report.
ARCH_MACHINES = {"x86_64": "x86_64", "aarch64": "aarch64"}

# Container image `Architecture` spellings each arch accepts (docker
# reports amd64/arm64; podman may report x86_64/aarch64).
IMAGE_ARCHES = {"x86_64": ("amd64", "x86_64"), "aarch64": ("arm64", "aarch64")}

# Artifact kinds the evidence bundle must carry.
ARTIFACT_KINDS = frozenset(
    {
        "closure-report",
        "action-key",
        "repomd-xml",
        "repomd-asc",
        "primary-xml",
        "provider-headers",
        "provider-rpm",
        "provider-origin",
        "provider-signature",
        "repomd-gpg-status",
    }
)

# `rpm -K` state that counts as signature acceptance.
SIGNATURE_OK = "gpg-ok"

# Distro repos enabled by default in the measuring container image
# (BaseOS/AppStream); the producer never enables or disables them.
# System providers may only come from these or the plan's build/system
# repo names. Anything else fails closed.
SYSTEM_IMPLICIT_REPOIDS = ("baseos", "appstream")

# Published-index repoids always carry this prefix (producer repo file).
PUBLISHED_REPOID_PREFIX = "tunaos-published-"

# ABI oracle kinds the producer may record. "unavailable" is NEVER
# admitted: an unmeasured ABI is a gap, not evidence.
ABI_ORACLES = frozenset(
    {
        "pkgconfig",
        "named-package",
        "grounded:gtk4-layer-shell",
        "grounded:libseat",
    }
)

_REQUIRE_VERSIONED = re.compile(
    r"\A(?P<cap>pkgconfig\([^()\s]+\)|[A-Za-z0-9][A-Za-z0-9+_.-]*)"
    r"\s*(?P<op>>=|<=|=|>|<)\s*(?P<ver>[^<>=!\s][^\s]*)\Z"
)


def _load_rpmvercmp():
    """Pure-Python rpmvercmp, loaded by path like gap_engine loads oci."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parent / "rpm_vercmp.py"
    spec = importlib.util.spec_from_file_location("el10_rpm_vercmp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.rpmvercmp


def compute_action_key(cell_id, arch, base_image, head_sha, workflow, plan_sha256,
                       receipt_sha256):
    """Bind the producer inputs to one key (cell-workflow convention).

    The workflow records this beside the artifacts (action-key.txt);
    admission recomputes it from the bound documents and compares.
    """
    for name, value in (
        ("cell_id", cell_id),
        ("arch", arch),
        ("base_image", base_image),
        ("head_sha", head_sha),
        ("workflow", workflow),
        ("plan_sha256", plan_sha256),
        ("receipt_sha256", receipt_sha256),
    ):
        require(_is_nonempty_str(value), f"action key input {name} missing")
    return canonical_sha256(
        {
            "action": "el10-closure",
            "arch": arch,
            "base_image": base_image,
            "cell_id": cell_id,
            "head_sha": head_sha,
            "plan_sha256": plan_sha256,
            "receipt_sha256": receipt_sha256,
            "workflow": workflow,
        }
    )


def _check_policy(policy):
    require(isinstance(policy, dict), "admission policy must be an object")
    heads = policy.get("trusted_heads")
    require(
        type(heads) is list and heads and all(_is_hex(h, 40) for h in heads),
        "policy trusted_heads must be a non-empty list of full commit SHAs",
    )
    workflow = policy.get("trusted_workflow")
    require(_is_nonempty_str(workflow), "policy trusted_workflow missing")
    keys = policy.get("trusted_keys")
    require(type(keys) is list and keys, "policy trusted_keys must be non-empty")
    keyids = {}
    for entry in keys:
        require(isinstance(entry, dict), "policy trusted_keys entry malformed")
        keyid = entry.get("keyid")
        require(
            type(keyid) is str and re.fullmatch(r"[0-9A-Fa-f]{8,40}", keyid or "") is not None,
            f"policy keyid malformed: {keyid!r}",
        )
        fingerprint = entry.get("fingerprint")
        require(type(fingerprint) is str and re.fullmatch(r"[0-9A-F]{40}", fingerprint),
                "policy requires full authoritative signing fingerprint")
        require(keyid.upper() == fingerprint, "policy trust identity must be the full fingerprint")
        keyids[fingerprint] = entry
    indexes = policy.get("expected_indexes")
    require(isinstance(indexes, dict) and indexes, "policy expected_indexes missing")
    for arch, urls in indexes.items():
        require(
            type(urls) is list and urls and all(_is_nonempty_str(u) for u in urls),
            f"policy expected_indexes[{arch}] must be a non-empty URL list",
        )
        require(len(set(urls)) == len(urls), f"policy expected_indexes[{arch}] duplicates")
    system_keys = policy.get("system_keys")
    require(isinstance(system_keys, dict) and system_keys, "missing source-pinned system signing policy")
    for repoid, fingerprints in system_keys.items():
        require(type(fingerprints) is list and fingerprints and all(
            type(v) is str and re.fullmatch(r"[0-9A-F]{40}", v) for v in fingerprints),
            "system signing policy requires full fingerprints")
    return {
        "system_keys": system_keys,
        "trusted_heads": set(heads),
        "trusted_workflow": workflow,
        "trusted_keyids": keyids,
        "expected_indexes": {arch: list(urls) for arch, urls in indexes.items()},
    }


def _split_versioned_require(requirement):
    """Split `cap [op version]`; fail closed on unparseable shapes."""
    require(_is_nonempty_str(requirement), "nameless require")
    match = _REQUIRE_VERSIONED.match(requirement.strip())
    if match is None:
        bare = requirement.strip()
        require(
            re.fullmatch(
                r"pkgconfig\([^()\s]+\)|[A-Za-z0-9][A-Za-z0-9+_.-]*", bare
            )
            is not None,
            f"require has an unusable shape: {requirement!r}",
        )
        return bare, None, None
    return match.group("cap"), match.group("op"), match.group("ver")


def _require_satisfied(cap, op, ver, abi):
    if op is None:
        return True
    require(isinstance(abi, dict), f"require {cap} lacks ABI observations")
    observed = abi.get("pkgconfig_version") if cap.startswith("pkgconfig(") else abi.get("evr")
    require(
        _is_nonempty_str(observed),
        f"require {cap} {op} {ver} has no observed version to judge",
    )
    rpmvercmp = _load_rpmvercmp()
    result = rpmvercmp(str(observed), ver)
    return {
        "=": result == 0,
        ">=": result >= 0,
        "<=": result <= 0,
        ">": result > 0,
        "<": result < 0,
    }[op]


def _check_platform_proof(proof, arch, base_image, cpu_baseline):
    require(isinstance(proof, dict), "report lacks native platform proof")
    require(proof.get("arch") == arch, "platform proof is for another arch")
    expected = ARCH_MACHINES[arch]
    container = proof.get("container_machine")
    runner = proof.get("runner_machine")
    require(
        container == expected,
        f"container reports {container!r}, want native {expected!r}",
    )
    require(
        runner == expected,
        f"runner reports {runner!r}, want native {expected!r}: emulated or cross claim",
    )
    require(
        proof.get("image_arch") in IMAGE_ARCHES[arch],
        f"image architecture {proof.get('image_arch')!r} does not match {arch}",
    )
    digest = base_image.partition("@sha256:")[2]
    require(
        _is_hex(proof.get("base_digest"), 64) and proof["base_digest"] == digest,
        "platform proof base digest is not the pinned base image digest",
    )
    require(
        _is_nonempty_str(proof.get("cpu_baseline"))
        and proof["cpu_baseline"] == cpu_baseline,
        "platform proof CPU baseline is not the staged cell baseline",
    )
    require(proof.get("os_id") == "centos" and proof.get("os_version", "").split(".",1)[0] == "10",
            "native subset requires actual CentOS Stream 10 OS identity")
    probe=proof.get("baseline_probe")
    require(isinstance(probe,dict) and probe.get("baseline")==cpu_baseline and
            probe.get("elf_machine")==({"x86_64":62,"aarch64":183}[arch]) and
            type(probe.get("execution_rc")) is int and probe["execution_rc"]==0 and
            _is_hex(probe.get("source_sha256"),64) and _is_hex(probe.get("executable_sha256"),64),
            "actual native ELF/baseline instruction probe missing")


def _check_repo_config(repo_config, plan_indexes):
    require(isinstance(repo_config, dict), "report lacks repo configuration evidence")
    require(repo_config.get("gpgcheck") is True, "report repos disabled signature checks")
    require(repo_config.get("repo_gpgcheck") is True, "report repos disabled metadata checks")
    require(_is_nonempty_str(repo_config.get("gpgkey")), "report repos lack a key identity")
    repos = repo_config.get("repos")
    require(type(repos) is list and repos, "report records no configured repos")
    seen = set()
    for repo in repos:
        require(isinstance(repo, dict), "report repo entry malformed")
        require(repo.get("gpgcheck") is True, f"repo {repo.get('name')!r} disabled signatures")
        require(repo.get("repo_gpgcheck") is True, f"repo {repo.get('name')!r} disabled metadata")
        baseurls = repo.get("baseurl")
        require(type(baseurls) is list and all(_is_nonempty_str(v) for v in baseurls),
                "actual repo baseurl observation malformed")
        require(baseurls or _is_nonempty_str(repo.get("metalink")), "actual repository source missing")
        require(type(repo.get("gpgkey")) is list and repo["gpgkey"], "actual repository key policy missing")
        require(repo.get("name") != "extras-common" or repo.get("includepkgs") == ["epel-release"],
                "Extras bootstrap-only package filter missing or broadened")
        seen.update(baseurls)
    require(
        set(plan_indexes) <= seen,
        f"plan indexes lack repo evidence: {sorted(set(plan_indexes) - seen)!r}",
    )


def _check_index_provenance(provenance, repourl):
    require(isinstance(provenance, dict), "observation lacks index provenance")
    require(provenance.get("baseurl") == repourl, "provenance baseurl is not the serving index")
    require(_is_nonempty_str(provenance.get("revision")), "provenance lacks a revision")
    require(_is_nonempty_str(provenance.get("primary_href")), "provenance lacks a primary href")
    observed = provenance.get("primary_sha256")
    algorithm=provenance.get("primary_checksum_algorithm")
    declared=provenance.get("primary_checksum_declared")
    require(_is_hex(observed,64), "provenance observed primary hash malformed")
    require(algorithm in ("sha256","sha512") and _is_hex(declared,64 if algorithm=="sha256" else 128),
            "actual signed primary checksum descriptor missing or weak")
    if algorithm=="sha256":
        require(provenance.get("primary_sha256_declared")==declared==observed,
                "served primary bytes differ from repomd declaration")
    else:
        require(provenance.get("primary_sha256_declared") is None,
                "SHA512 primary must not invent a declared SHA256 checksum")
    require(
        _is_strict_int(provenance.get("primary_bytes")) and provenance["primary_bytes"] > 0,
        "provenance primary size unmeasured",
    )


def _consistent_signer(signature):
    fingerprint=signature.get("fingerprint")
    keyid=signature.get("keyid")
    require(type(keyid) is str and re.fullmatch(r"[0-9A-Fa-f]{16,40}",keyid) is not None and
            type(fingerprint) is str and fingerprint.endswith(keyid.upper()),
            "untrusted key ID inconsistent with verified full fingerprint")


def _check_selected(selected, arch, plan_indexes, plan_repo_names, trusted_keyids,
                    keyring):
    require(isinstance(selected, dict), "observation lacks a selected provider")
    require(_is_nonempty_str(selected.get("nevra")), "selected provider lacks a NEVRA")
    require(_is_nonempty_str(selected.get("evr")), "selected provider lacks an EVR")
    require(selected.get("arch") in (arch, "noarch"), f"provider {selected.get('nevra')!r} is not {arch}")
    require(selected["nevra"].endswith("."+selected["arch"]), "provider NEVRA architecture disagrees with selected header")
    repoid = selected.get("repoid")
    require(_is_nonempty_str(repoid), "selected provider lacks a repoid")
    repourl = selected.get("repourl")
    if repoid.startswith(PUBLISHED_REPOID_PREFIX):
        require(
            repourl in plan_indexes,
            f"provider {selected.get('nevra')!r} served outside the plan indexes",
        )
        branch = "published-index"
    else:
        require(_is_nonempty_str(repourl), f"provider {selected.get('nevra')!r} lacks a mirror URL")
        require(
            repoid in set(plan_repo_names) | set(SYSTEM_IMPLICIT_REPOIDS),
            f"provider {selected.get('nevra')!r} served from unexpected repo {repoid!r}",
        )
        branch = "system-repo"
    require(_is_nonempty_str(selected.get("sourcerpm")), "selected provider lacks a SRPM")
    require(_is_hex(selected.get("sha256header"), 64), "selected provider header hash malformed")
    require(_is_hex(selected.get("rpm_sha256"), 64), "selected provider RPM bytes unhashed")
    signature = selected.get("signature")
    require(isinstance(signature, dict), "selected provider lacks signature evidence")
    require(
        signature.get("state") == SIGNATURE_OK,
        f"provider {selected.get('nevra')!r} signature state {signature.get('state')!r}",
    )
    fingerprint = signature.get("fingerprint")
    allowed = trusted_keyids if branch == "published-index" else keyring.get(repoid, [])
    require(type(fingerprint) is str and fingerprint in allowed,
            f"provider {selected.get('nevra')!r} signed by untrusted fingerprint")
    require(signature.get("cryptographic_signature_verified") is True and
            signature.get("digests_verified") is True,
            "actual RPM digital signature and digest verification missing")
    _consistent_signer(signature)
    require(_is_nonempty_str(signature.get("key")), "provider signature lacks a key identity")
    return repourl, branch


def _check_served_from(entry, selected, repourl, branch):
    served = entry.get("served_from")
    require(isinstance(served, dict), "observation lacks serving-repo evidence")
    require(served.get("kind") == branch, "serving-repo kind disagrees with the repoid")
    require(served.get("repoid") == selected["repoid"], "serving repoid disagrees")
    if branch == "published-index":
        require(served.get("repourl") == repourl, "serving URL disagrees with selection")
        _check_index_provenance(entry.get("index_provenance"), repourl)
    else:
        require(
            served.get("baseurl") == repourl,
            "system mirror URL disagrees with selection",
        )
        require(_is_nonempty_str(served.get("revision")), "system repo revision unmeasured")
        require(_is_nonempty_str(served.get("updated")), "system repo timestamp unmeasured")
        _check_index_provenance(entry.get("index_provenance"), repourl)


def _check_abi(abi, kind, capability):
    require(isinstance(abi, dict), "observation lacks ABI evidence")
    expected = ("grounded:gtk4-layer-shell" if capability == "pkgconfig(gtk4-layer-shell-0)" else
                "grounded:libseat" if capability == "pkgconfig(libseat)" else
                "pkgconfig" if capability.startswith("pkgconfig(") else "named-package")
    require(abi.get("oracle") == expected, "wrong capability ABI oracle")
    require(type(abi.get("files_count")) is int and abi["files_count"] > 0,
            "installed package payload not observed")
    for field in ("headers", "sonames"):
        require(type(abi.get(field)) is list, "missing actual ABI file observation")
    loaded = abi.get("dlopen")
    require(isinstance(loaded, dict) and set(loaded) == set(abi["sonames"]) and
            all(v is True for v in loaded.values()), "failed or incomplete actual SONAME loading")
    ownership = abi.get("file_ownership")
    require(isinstance(ownership, dict) and ownership and all(
        isinstance(v, dict) and v.get("readable") is True and
        _is_nonempty_str(v.get("owner_nevra")) and _is_hex(v.get("sha256"),64)
        for v in ownership.values()), "missing installed ABI file bytes/owner/readability")
    if expected != "named-package":
        require(_is_nonempty_str(abi.get("pkgconfig_version")), "missing observed pkgconfig version")
        require(type(abi.get("provides")) is list and any(
            value.startswith(capability + " = ") for value in abi["provides"]),
            "provider does not advertise the measured capability")
        require(abi.get("pc_file") in ownership and set(abi["headers"]) <= set(ownership),
                "pkgconfig/header actual file owners missing")
        require(abi.get("sdk_compile_and_run") is True, "actual native SDK compile/run failed")
    else:
        require(_is_nonempty_str(abi.get("evr")), "named package EVR missing")
        require(abi.get("smoke_required") is False or (type(abi.get("smoke")) is list and
            abi["smoke"] and any(v.get("rc") == 0 for v in abi["smoke"])), "named command smoke failed")
    if expected == "grounded:gtk4-layer-shell":
        require(abi.get("pkgconfig_version") == "1.3.0" and abi.get("library_version") == [1,3,0],
                "actual recipe/library GTK layer-shell version differs")
        require(abi.get("symbols_resolved") is True and abi.get("symbols_sample") == [
            "gtk_layer_init_for_window", "gtk_layer_set_keyboard_mode", "gtk_layer_set_namespace",
            "gtk_session_lock_instance_new", "gtk_session_lock_instance_lock", "gtk_session_lock_instance_unlock",
            "gtk_session_lock_instance_assign_window_to_monitor", "gtk_session_lock_instance_is_locked"],
            "missing or failed exact grounded GTK symbols")
        for field in ("headers_present", "typelibs_present"):
            require(isinstance(abi.get(field),dict) and len(abi[field]) == 2 and
                    all(v is True for v in abi[field].values()) and
                    set(abi[field]) <= set(ownership), "missing or failed grounded GTK payload")
        require(abi.get("license_present") is True and
                abi.get("license_path") == "/usr/share/licenses/gtk4-layer-shell/LICENSE" and
                abi["license_path"] in ownership, "GTK layer-shell license missing or unowned")
    if expected == "grounded:libseat":
        require(type(abi.get("seatd_smoke_rc")) is int and abi["seatd_smoke_rc"] == 0,
                "actual seatd smoke failed")


def _check_observation(requirement, kind, observed, arch, plan_indexes, plan_repo_names,
                       trusted_keyids, keyring):
    require(type(observed) is list and observed, f"require {requirement} has no observations")
    cap, op, ver = _split_versioned_require(requirement)
    for entry in observed:
        require(isinstance(entry, dict), f"require {requirement} observation malformed")
        require(entry.get("require") == requirement, "observation bound to another require")
        require(entry.get("kind") == kind, f"observation kind is not {kind}")
        require(entry.get("status") == "resolved", f"require {requirement} unresolved")
        providers = entry.get("providers")
        require(
            type(providers) is list and providers and all(_is_nonempty_str(p) for p in providers),
            f"require {requirement} records no provider candidates",
        )
        selected = entry.get("selected")
        repourl, branch = _check_selected(
            selected, arch, plan_indexes, plan_repo_names, trusted_keyids, keyring
        )
        require(
            selected["nevra"] in providers,
            f"selected {selected['nevra']!r} is not a recorded candidate",
        )
        _check_served_from(entry, selected, repourl, branch)
        index_signature=entry.get("index_signature")
        allowed=trusted_keyids if branch=="published-index" else keyring.get(selected["repoid"],[])
        require(isinstance(index_signature,dict) and index_signature.get("verified") is True and
                index_signature.get("signer_fingerprint") in allowed,
                "actual signed provider index verification missing")
        abi = entry.get("abi")
        _check_abi(abi, kind, cap)
        if selected["arch"]=="noarch":
            require(abi.get("architecture_neutral_payload") is True and
                    type(abi.get("noarch_regular_files_checked")) is int and
                    abi["noarch_regular_files_checked"]>0, "noarch ELF/native payload not neutral")
        constraint = entry.get("constraint")
        if op is None:
            require(constraint is None, f"bare require {requirement} carries a constraint")
        else:
            require(isinstance(constraint, dict), f"require {requirement} lacks a verdict")
            require(
                constraint.get("op") == op and constraint.get("version") == ver,
                f"require {requirement} verdict judges another constraint",
            )
            require(constraint.get("satisfied") is True, f"require {requirement} unsatisfied")
            require(
                _require_satisfied(cap, op, ver, abi),
                f"require {requirement} observed version fails the constraint",
            )
    return observed[0]["selected"]


def _check_signatures_measured(signatures, trusted_keyids, selected_by_nevra, plan_indexes):
    _check_signatures(signatures)
    keyid = signatures.get("fingerprint")
    require(
        type(keyid) is str and keyid.upper() in trusted_keyids,
        f"report declares an untrusted key identity {keyid!r}",
    )
    _consistent_signer(signatures)
    rpm_k = signatures.get("rpm_K")
    require(type(rpm_k) is list and rpm_k, "report lacks per-RPM signature states")
    seen = {}
    for entry in rpm_k:
        require(isinstance(entry, dict), "rpm_K entry malformed")
        seen[entry.get("nevra")] = entry
    for nevra, selected in selected_by_nevra.items():
        entry = seen.get(nevra)
        require(entry is not None, f"provider {nevra!r} lacks an rpm_K state")
        require(entry.get("state") == SIGNATURE_OK, f"provider {nevra!r} rpm_K not accepted")
        require(
            type(entry.get("keyid")) is str
            and entry.get("fingerprint") == selected["signature"]["fingerprint"],
            f"provider {nevra!r} rpm_K key disagrees with observation",
        )
    asc = signatures.get("repomd_asc")
    require(isinstance(asc, dict) and asc, "report lacks repomd signature states")
    for baseurl in plan_indexes:
        state = asc.get(baseurl)
        require(isinstance(state, dict), f"index {baseurl} lacks a repomd signature state")
        require(state.get("present") is True, f"index {baseurl} repomd signature missing")
        require(state.get("verified") is True, f"index {baseurl} repomd signature unverified")
        require(
            type(state.get("signer_fingerprint")) is str
            and state["signer_fingerprint"] in trusted_keyids,
            f"index {baseurl} signed by an untrusted key",
        )


def _check_artifacts(artifacts, run_id, plan_indexes, requires, factory_repoids):
    require(type(artifacts) is list and artifacts, "report binds no artifacts")
    by_kind: dict = {}
    for entry in artifacts:
        require(isinstance(entry, dict), "artifact entry malformed")
        require(_is_nonempty_str(entry.get("name")), "nameless artifact entry")
        require(entry.get("kind") in ARTIFACT_KINDS, f"artifact kind {entry.get('kind')!r} unknown")
        require(_is_hex(entry.get("sha256"), 64), f"artifact {entry.get('name')!r} unhashed")
        require(entry.get("run_id") == run_id, f"artifact {entry.get('name')!r} from another run")
        by_kind.setdefault(entry["kind"], []).append(entry)
    require("closure-report" in by_kind, "report binds no closure-report artifact")
    require("action-key" in by_kind, "report binds no action-key artifact")
    for baseurl in plan_indexes:
        for kind in ("repomd-xml", "repomd-asc", "primary-xml"):
            require(
                any(e.get("index_url") == baseurl for e in by_kind.get(kind, [])),
                f"index {baseurl} lacks a bound {kind} artifact",
            )
    header_requires = {
        e.get("require") for e in by_kind.get("provider-headers", [])
    }
    for requirement in requires:
        require(requirement in header_requires, f"require {requirement} lacks header evidence")
    for requirement in requires:
        require(any(e.get("require")==requirement for e in by_kind.get("provider-rpm",[])),
                f"require {requirement} lacks exact provider RPM bytes")
        for kind in ("provider-origin","provider-signature"):
            require(sum(e.get("require")==requirement for e in by_kind.get(kind,[]))==1,
                    f"require {requirement} lacks exactly one original {kind} artifact")


def validate_producer_evidence(report, plan, receipt, policy):
    """Validate local native observations; this never authenticates their origin.

    Verifies, in order: policy shape; subset receipt validity; plan
    index-set identity (this arch only); plan/receipt/source hash
    bindings; draft report shape; exact require coverage; producer run
    binding (run_id suffix, trusted head, workflow, action key);
    native platform proof; enforced signature configuration; exhaustive
    per-require provider/ABI observations with re-judged versioned
    constraints; measured signatures; and artifact bindings. Returns an
    evidence summary on success; raises ValueError naming the gap on
    anything less.
    """
    trusted = _check_policy(policy)
    require(isinstance(report, dict), "report must be an object")
    require(isinstance(plan, dict), "plan must be an object")
    source_sha, cells = validate_candidate_receipt(receipt)
    cell_key = report.get("cell")
    cell = next(
        (c for c in cells if f"{c['consumer']}-{c['target']}-{c['arch']}" == cell_key),
        None,
    )
    require(cell is not None, "report cell is not a staged subset cell")
    arch = cell["arch"]
    require(report.get("arch") == arch, "report arch is not the cell arch")
    require(plan.get("cell") == cell_key, "plan is for another cell")
    require(plan.get("arch") == arch, "plan arch is not the cell arch")
    base_image = cell.get("base_image")
    require(report.get("base_image") == base_image, "report base is not the staged base")
    for cell_key_name, plan_key in (("build_requires", "build"),
                                  ("runtime_requires", "runtime")):
        staged = cell.get(cell_key_name)
        planned = plan.get("requires", {}).get(plan_key) or []
        require(
            type(staged) is list and sorted(staged) == sorted(planned),
            f"plan {plan_key} requires are not the staged cell require sets",
        )
    plan_indexes = plan.get("repos", {}).get("published_index")
    require(
        type(plan_indexes) is list and plan_indexes,
        "plan declares no served index for this cell",
    )
    expected = trusted["expected_indexes"].get(arch)
    require(expected is not None, f"policy names no served index for {arch}")
    require(
        set(plan_indexes) == set(expected),
        f"plan index set {sorted(plan_indexes)!r} is not the trusted {arch} set",
    )
    for other_arch, other_urls in trusted["expected_indexes"].items():
        if other_arch == arch:
            continue
        leaked = set(plan_indexes) & set(other_urls)
        require(not leaked, f"plan carries {other_arch} supply: {sorted(leaked)!r}")
    require(
        report.get("plan_sha256") == canonical_sha256(plan),
        "report plan binding does not match the plan bytes",
    )
    require(
        report.get("receipt_sha256") == canonical_sha256(receipt),
        "report receipt binding does not match the receipt bytes",
    )
    require(report.get("source_sha") == source_sha, "report source is not the staged source")
    validate_closure_report(report, cell_key)
    requires = plan.get("requires", {})
    for kind in ("build", "runtime"):
        require(
            sorted(report["covers_requires"].get(kind) or []) == sorted(requires.get(kind) or []),
            f"report does not cover exactly the plan {kind} requires",
        )
    producer = report.get("producer")
    run_id = producer.get("run_id")
    require(
        producer.get("run_url")
        == f"https://github.com/tuna-os/tunaos-packages/actions/runs/{run_id}",
        "producer run_id is not bound to the run_url suffix",
    )
    head_sha = producer.get("head_sha")
    require(
        _is_hex(head_sha, 40) and head_sha in trusted["trusted_heads"],
        "producer head is not a trusted head",
    )
    require(
        producer.get("workflow") == trusted["trusted_workflow"],
        "producer workflow is not the trusted producer",
    )
    require(producer.get("cell_id") == cell_key, "producer cell binding mismatch")
    require(
        producer.get("action_key")
        == compute_action_key(
            cell_key,
            arch,
            base_image,
            head_sha,
            trusted["trusted_workflow"],
            report["plan_sha256"],
            report["receipt_sha256"],
        ),
        "producer action key does not recompute from the bound inputs",
    )
    _check_platform_proof(
        report.get("platform_proof"), arch, base_image, cell.get("cpu_baseline")
    )
    _check_repo_config(report.get("repo_config"), plan_indexes)
    keyring = report.get("rpm_keyring")
    require(type(keyring) is list and keyring, "report lacks container keyring evidence")
    # Container key observations are evidence only, never authority.
    keyring_ids = trusted["system_keys"]
    dependencies = report.get("dependencies")
    plan_repo_names = list(plan.get("repos", {}).get("build", [])) + list(
        plan.get("repos", {}).get("system", [])
    )
    selected_by_nevra = {}
    for kind in ("build", "runtime"):
        for requirement in requires[kind]:
            selected = _check_observation(
                requirement, kind, dependencies.get(requirement), arch,
                plan_indexes, plan_repo_names, trusted["trusted_keyids"], keyring_ids,
            )
            selected_by_nevra[selected["nevra"]] = selected
    require(
        set(dependencies) == set(requires["build"]) | set(requires["runtime"]),
        "report observes requires outside the plan",
    )
    _check_signatures_measured(
        report.get("signatures"), trusted["trusted_keyids"], selected_by_nevra, plan_indexes
    )
    factory_repoids = {
        selected["repoid"]
        for selected in selected_by_nevra.values()
        if selected["repoid"].startswith(PUBLISHED_REPOID_PREFIX)
    }
    _check_artifacts(
        report.get("artifacts"),
        run_id,
        plan_indexes,
        list(requires["build"]) + list(requires["runtime"]),
        factory_repoids,
    )
    packages = {entry.get("require"): entry for entry in report.get("packages")}
    for requirement in list(requires["build"]) + list(requires["runtime"]):
        entry = packages.get(requirement)
        require(entry is not None, f"require {requirement} lacks a package verdict")
        require(entry.get("status") == "resolved", f"require {requirement} package unresolved")
    return {
        "cell": cell_key,
        "arch": arch,
        "run_id": run_id,
        "head_sha": head_sha,
        "requires_observed": len(requires["build"]) + len(requires["runtime"]),
        "providers": sorted(selected_by_nevra),
        "indexes": sorted(plan_indexes),
        "artifacts": len(report["artifacts"]),
    }


def admit_producer_closure(report, plan, receipt, policy, *, custody=None, artifact_dir=None):
    """Actual admission requires independent original API/ZIP/source custody.

    The local semantic validator is separately named and never authenticates a
    run. There is no report/JSON switch to disable this prerequisite.
    """
    import importlib.util
    import pathlib
    import sys
    here = pathlib.Path(__file__).resolve().parent
    path = here / "el10-producer-custody.py"
    name = "el10_el10-producer-custody_" + __import__("hashlib").sha256(str(path).encode()).hexdigest()[:12]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name,path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    authority = sys.modules[name]
    require(isinstance(custody,authority.VerifiedCustody), "independent original producer custody required")
    producer = report.get("producer",{})
    require(producer.get("head_sha")==custody.head and producer.get("run_id")==custody.run and
            producer.get("run_attempt")==custody.attempt, "report differs from independently admitted run")
    require(custody.report_hashes.get(report.get("cell")) == canonical_sha256(report),
            "report differs from independently admitted original archive payload")
    require(policy.get("trusted_heads") == [custody.head] and
            policy.get("trusted_workflow") == authority.WORKFLOW,
            "policy does not identify the independently admitted producer")
    require(artifact_dir is not None, "original provider evidence bytes required")
    io_path = here / "el10-evidence.py"
    spec=importlib.util.spec_from_file_location("el10_admission_io",io_path)
    io=importlib.util.module_from_spec(spec);spec.loader.exec_module(io)
    observed=io.verify_bundle(artifact_dir)
    require(observed==report, "actual artifact report differs")
    pins = io.load_json(here / "el10-keys/policy.json", 65536)
    require(policy.get("key_policy_sha256") == custody.sources.get("scripts/el10-keys/policy.json") ==
            io.digest_file(here / "el10-keys/policy.json", 65536),
            "policy differs from admitted original source key policy")
    import yaml
    factory_path=here.parent / "manifests/package-factory.yaml"
    require(io.digest_file(factory_path,4*1024*1024)==custody.sources.get("manifests/package-factory.yaml"),
            "executing factory policy differs from original producer source")
    factory=yaml.safe_load(io.read_regular(factory_path,4*1024*1024))
    declared=factory["targets"]["el10"]["published_index"]
    expected_indexes={arch: [urls] if isinstance(urls,str) else list(urls)
                      for arch,urls in declared.items()}
    require(policy.get("expected_indexes")==expected_indexes,
            "caller policy changes original source-pinned served indexes")
    expected_factory = [{"keyid": v, "fingerprint": v}
                        for v in pins["factory"]["signing_fingerprints"]]
    expected_system = {repo: entry["signing_fingerprints"] for entry in pins["keys"]
                       if entry.get("scope") != "bootstrap-only" for repo in entry["repositories"]}
    require(policy.get("trusted_keys") == expected_factory and
            policy.get("system_keys") == expected_system,
            "caller policy adds or substitutes authoritative signing identities")
    io.validate_provider_artifacts(report,artifact_dir,policy,deadline=custody.deadline)
    summary=validate_producer_evidence(report,plan,receipt,policy)
    return dict(summary, scope="original two-architecture native producer admission; not full variant release",
                original_archive_sha256=dict(custody.archive_hashes))
