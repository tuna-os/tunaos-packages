"""Measured EL10 producer admission: fixture-driven unit tests.

Stdlib only, no pytest/yaml imports: runs under pytest AND directly
via `/usr/bin/python3 tests/test_el10_producer_admission.py`.

These are local semantic controls, not authenticated producer admission.
Every adversarial fixture must FAIL local validation; exactly one
fully-observed control per arch must PASS. Original API admission separately
requires independently verified source and artifact custody. Fixture require sets,
index URLs, and key identities are transcribed from production source
at f1965b3 (factory blob e3c78f05, stager, public.gpg blob 275f4077);
EVRs, hashes, and signatures are deterministic fixture values, never
presented as measurements.
"""

import hashlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


closure = _load("closure", "scripts/el10-rpm-closure.py")
producer = _load("producer", "scripts/el10-measure-closure.py")

# Require sets transcribed from stage-tuna-desktop-el10.py (BUILD,
# RUNTIME) plus catalog-mapped capabilities (rust, cargo, gcc,
# pkgconf-pkg-config) at f1965b3.
MAPPED = ["rust", "cargo", "gcc", "pkgconf-pkg-config"]
BUILD = [
    "clang-devel",
    "pkgconfig(dbus-1)",
    "pkgconfig(glib-2.0)",
    "pkgconfig(gtk4) >= 4.12",
    "pkgconfig(gtk4-layer-shell-0) >= 1.1",
    "pkgconfig(libadwaita-1)",
    "pkgconfig(libdrm)",
    "pkgconfig(libinput)",
    "pkgconfig(libsecret-1)",
    "pkgconfig(xkbcommon)",
    "pkgconfig(xkbcommon-x11)",
    "pkgconfig(gbm)",
    "pkgconfig(libpipewire-0.3)",
    "pkgconfig(libseat)",
    "pkgconfig(libsystemd)",
    "pkgconfig(libudev)",
    "pkgconfig(egl)",
    "pkgconfig(wayland-client)",
    "pkgconfig(wayland-server)",
    "wayland-protocols-devel",
]
RUNTIME = [
    "dbus",
    "dconf",
    "pam",
    "gsettings-desktop-schemas",
    "libcanberra-gtk3",
    "libcanberra-pulseaudio",
    "sound-theme-freedesktop",
    "xdg-desktop-portal",
    "xdg-desktop-portal-gtk",
]
BUILD_REQUIRES = MAPPED + BUILD

# Served indexes transcribed from factory targets.el10 at f1965b3.
INDEXES = {
    "x86_64": [
        "https://repo.tunaos.org/repo/10/x86_64/",
        "https://repo.tunaos.org/xfce/10-stream-x86_64/",
    ],
    "aarch64": ["https://repo.tunaos.org/rpm/el10/aarch64/"],
}

# Factory key identities decoded from public.gpg (blob 275f4077):
# primary 629BE6EA45188366, signing subkey F7A5375898C78B31.
FACTORY_SUBKEY = "F7A5375898C78B31"
FACTORY_SUBKEY_SHORT = "98C78B31"
FACTORY_PRIMARY = "629BE6EA45188366"
FACTORY_FPR = "D0ED0FEA5E5BFE3324DFCDACF7A5375898C78B31"
KEY_NAME = "RPM Signing <rpm-signing@tuna-os.github.com>"

# Fixture distro keyring identity (clearly synthetic hex).
DISTRO_KEYID = "AAAA1111BBBB2222"
DISTRO_FPR = "111111111111111111111111" + DISTRO_KEYID

HEAD = "f" * 40
SOURCE_SHA = "c" * 40
RUN_ID = 12345
RUN_URL = f"https://github.com/tuna-os/tunaos-packages/actions/runs/{RUN_ID}"
WORKFLOW = "el10-closure-producer.yml"
BASELINES = {"x86_64": "x86-64-v3", "aarch64": "armv8-a"}
PLATFORMS = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}
MACHINES = {"x86_64": "x86_64", "aarch64": "aarch64"}
IMAGE_ARCHES = {"x86_64": "amd64", "aarch64": "arm64"}
SYSTEM_REPOIDS = ["appstream", "baseos", "crb", "epel"]

# Factory-served requires in the positive fixture (recipes with el10
# targets at f1965b3: gtk4-layer-shell, libseat, wayland-protocols).
FACTORY_SERVED = {
    "pkgconfig(gtk4-layer-shell-0) >= 1.1": ("gtk4-layer-shell-devel", "1.3.0", "1.el10"),
    "pkgconfig(libseat)": ("libseat-devel", "0.9.0", "1.el10"),
    "wayland-protocols-devel": ("wayland-protocols-devel", "1.49", "1.el10"),
}


def fixture_hash(*parts):
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def provider_name_version(require):
    if require in FACTORY_SERVED:
        return FACTORY_SERVED[require]
    if require.startswith("pkgconfig("):
        module = require[len("pkgconfig("):].split(")")[0]
        if module == "gtk4":
            return ("gtk4-devel", "4.14.0", "1.el10")
        if module == "dbus-1":
            return ("dbus-devel", "1.14.0", "1.el10")
        stem = module.replace("lib", "").strip("-") or module
        return (f"{module}-devel".replace("--", "-"), "2.0", "1.el10", stem)[:3]
    return (require, "1.0", "1.el10")


def make_receipt():
    cells = []
    for arch in ("x86_64", "aarch64"):
        digest = fixture_hash("base", arch)
        cells.append(
            {
                "consumer": "skipjack",
                "target": "el10",
                "arch": arch,
                "base_image": f"quay.io/centos/centos@sha256:{digest}",
                "base_reference": "quay.io/centos/centos:stream10",
                "platform": PLATFORMS[arch],
                "recipe_sha256": fixture_hash("recipe", arch),
                "cpu_baseline": BASELINES[arch],
                "build_requires": list(BUILD_REQUIRES),
                "runtime_requires": list(RUNTIME),
            }
        )
    return {
        "schema": 1,
        "candidate_only": True,
        "release_qualified": False,
        "published": False,
        "source_sha": SOURCE_SHA,
        "source_archive_sha256": fixture_hash("source"),
        "vendor_archive_sha256": fixture_hash("vendor"),
        "lock_sha256": fixture_hash("lock"),
        "cell_subset": ["skipjack"],
        "cells": cells,
    }


def target_cfg():
    return {
        "status": "supported",
        "format": "rpm",
        "buildroot": "epel-10",
        "architectures": ["x86_64", "aarch64"],
        "published_index": {
            "x86_64": list(INDEXES["x86_64"]),
            "aarch64": INDEXES["aarch64"][0],
        },
        "build_repositories": ["crb", "epel"],
        "system_repositories": ["crb", "epel"],
    }


def make_plan(receipt, arch):
    cell = next(c for c in receipt["cells"] if c["arch"] == arch)
    return closure.plan_base_closure(cell, target_cfg(), BUILD_REQUIRES, RUNTIME)


def make_policy():
    return {
        "trusted_heads": [HEAD],
        "trusted_workflow": WORKFLOW,
        "trusted_keys": [{"keyid": FACTORY_FPR, "fingerprint": FACTORY_FPR}],
        "system_keys": {repo:[DISTRO_FPR] for repo in SYSTEM_REPOIDS},
        "expected_indexes": {
            "x86_64": list(INDEXES["x86_64"]),
            "aarch64": list(INDEXES["aarch64"]),
        },
    }


def abi_for(require, name, evr):
    if require == "pkgconfig(gtk4-layer-shell-0) >= 1.1":
        return {
            "oracle": "grounded:gtk4-layer-shell",
            "pkgconfig_version": "1.3.0",
            "provides": ["pkgconfig(gtk4-layer-shell-0) = 1.3.0"],
            "headers": ["/usr/include/gtk4-layer-shell/gtk4-layer-shell.h"],
            "sonames": ["libgtk4-layer-shell.so.0"],
            "symbols_sample": ["gtk_layer_init_for_window", "gtk_session_lock_instance_lock"],
        }
    if require == "pkgconfig(libseat)":
        return {
            "oracle": "grounded:libseat",
            "pkgconfig_version": "0.9.0",
            "provides": ["pkgconfig(libseat) = 0.9.0"],
            "headers": ["/usr/include/libseat.h"],
            "sonames": ["libseat.so.1"],
        }
    if require.startswith("pkgconfig("):
        module = require[len("pkgconfig("):].split(")")[0]
        version = "4.14.0" if module == "gtk4" else "2.0"
        return {
            "oracle": "pkgconfig",
            "pkgconfig_version": version,
            "provides": [f"pkgconfig({module}) = {version}"],
            "headers": [f"/usr/include/{module}.h"],
            "sonames": [],
        }
    return {
        "oracle": "named-package",
        "evr": evr,
        "provides_count": 12,
        "provides_sample": [name],
        "files_count": 40,
        "headers": [],
        "sonames": [],
        "smoke": [],
    }


def make_observation(require, kind, arch, slot):
    name, version, release = provider_name_version(require)
    evr = f"0:{version}-{release}"
    nevra = f"{name}-{version}-{release}.{arch}"
    factory = require in FACTORY_SERVED
    if factory:
        repoid = "tunaos-published-0"
        repourl = INDEXES[arch][0]
        served_from = {"kind": "published-index", "repoid": repoid, "repourl": repourl}
        primary = fixture_hash("primary", arch)
        provenance = {
            "baseurl": repourl,
            "revision": f"rev-{arch}",
            "primary_href": "repodata/primary.xml.gz",
            "primary_sha256": primary,
            "primary_sha256_declared": primary,
            "primary_bytes": 1000 + slot,
        }
        keyid = FACTORY_SUBKEY
    else:
        repoid = SYSTEM_REPOIDS[slot % len(SYSTEM_REPOIDS)]
        repourl = f"https://mirror.example/{arch}/{repoid}/"
        served_from = {
            "kind": "system-repo",
            "repoid": repoid,
            "baseurl": repourl,
            "revision": f"sysrev-{repoid}",
            "updated": "2026-10-01T00:00:00Z",
        }
        provenance = None
        keyid = DISTRO_KEYID
    constraint = None
    if require == "pkgconfig(gtk4) >= 4.12":
        constraint = {"op": ">=", "version": "4.12", "observed": "4.14.0",
                      "satisfied": True, "dnf_arbitrated": True}
    elif require == "pkgconfig(gtk4-layer-shell-0) >= 1.1":
        constraint = {"op": ">=", "version": "1.1", "observed": "1.3.0",
                      "satisfied": True, "dnf_arbitrated": True}
    entry = {
        "require": require,
        "kind": kind,
        "status": "resolved",
        "providers": [nevra],
        "selected": {
            "nevra": nevra,
            "evr": evr,
            "arch": arch,
            "repoid": repoid,
            "repourl": repourl,
            "sourcerpm": f"{name}-{version}-{release}.src.rpm",
            "sha256header": fixture_hash("header", nevra),
            "rpm_sha256": fixture_hash("rpm", nevra),
            "signature": {"state": "gpg-ok", "keyid": keyid, "key": KEY_NAME},
            "dnf_arbitrated": True,
        },
        "served_from": served_from,
        "abi": abi_for(require, name, evr),
        "constraint": constraint,
    }
    if provenance is not None:
        entry["index_provenance"] = provenance
    return entry


def make_report(plan, receipt, arch):
    dependencies = {}
    packages = []
    rpm_k = []
    slot = 0
    for kind in ("build", "runtime"):
        for require in plan["requires"][kind]:
            entry = make_observation(require, kind, arch, slot)
            slot += 1
            dependencies[require] = [entry]
            selected = entry["selected"]
            packages.append(
                {"name": selected["nevra"].rsplit("-", 2)[0], "require": require,
                 "status": "resolved", "evr": selected["evr"]}
            )
            rpm_k.append(
                {"nevra": selected["nevra"], "state": "gpg-ok",
                 "keyid": selected["signature"]["keyid"]}
            )
    base_image = next(c for c in receipt["cells"] if c["arch"] == arch)["base_image"]
    digest = base_image.partition("@sha256:")[2]
    artifacts = [
        {"name": "report.json", "kind": "closure-report",
         "sha256": fixture_hash("artifact", "report", arch), "run_id": RUN_ID},
        {"name": "action-key.txt", "kind": "action-key",
         "sha256": fixture_hash("artifact", "key", arch), "run_id": RUN_ID},
    ]
    for index, baseurl in enumerate(plan["repos"]["published_index"]):
        for kind, suffix in (("repomd-xml", "xml"), ("repomd-asc", "asc"),
                             ("primary-xml", "bin")):
            artifacts.append(
                {"name": f"evidence/idx-{index}.{suffix}", "kind": kind,
                 "sha256": fixture_hash("artifact", arch, str(index), kind),
                 "run_id": RUN_ID, "index_url": baseurl}
            )
    for require in list(plan["requires"]["build"]) + list(plan["requires"]["runtime"]):
        artifacts.append(
            {"name": f"evidence/headers-{fixture_hash(require)[:8]}.json",
             "kind": "provider-headers",
             "sha256": fixture_hash("artifact", "headers", require),
             "run_id": RUN_ID, "require": require}
        )
    artifacts.append(
        {"name": "evidence/factory.rpm", "kind": "provider-rpm",
         "sha256": fixture_hash("artifact", "rpm", arch), "run_id": RUN_ID,
         "require": "pkgconfig(gtk4-layer-shell-0) >= 1.1",
         "repoid": "tunaos-published-0"}
    )
    plan_sha = closure.canonical_sha256(plan)
    receipt_sha = closure.canonical_sha256(receipt)
    return {
        "schema": 1,
        "cell": plan["cell"],
        "arch": arch,
        "plan_sha256": plan_sha,
        "receipt_sha256": receipt_sha,
        "source_sha": SOURCE_SHA,
        "base_image": base_image,
        "base_reference": "quay.io/centos/centos:stream10",
        "producer": {
            "run_id": RUN_ID,
            "run_url": RUN_URL,
            "head_sha": HEAD,
            "workflow": WORKFLOW,
            "cell_id": plan["cell"],
            "action_key": closure.compute_action_key(
                plan["cell"], arch, base_image, HEAD, WORKFLOW, plan_sha, receipt_sha
            ),
        },
        "platform_proof": {
            "arch": arch,
            "container_machine": MACHINES[arch],
            "runner_machine": MACHINES[arch],
            "image_arch": IMAGE_ARCHES[arch],
            "base_digest": digest,
            "cpu_baseline": BASELINES[arch],
            "os_id": "centos",
            "os_version": "10",
        },
        "repo_config": {
            "gpgcheck": True,
            "repo_gpgcheck": True,
            "gpgkey": "file:///etc/pki/rpm-gpg/TUNAOS-EL10",
            "repos": [
                {"name": f"tunaos-published-{i}", "baseurl": url,
                 "gpgcheck": True, "repo_gpgcheck": True}
                for i, url in enumerate(plan["repos"]["published_index"])
            ],
        },
        "rpm_keyring": [{"keyid": FACTORY_SUBKEY}, {"keyid": DISTRO_KEYID}],
        "signatures": {
            "signed_by": "tunaos-factory",
            "key": KEY_NAME,
            "keyid": FACTORY_SUBKEY,
            "rpm_K": rpm_k,
            "repomd_asc": {
                url: {"present": True, "verified": True, "signer_keyid": FACTORY_SUBKEY}
                for url in plan["repos"]["published_index"]
            },
        },
        "dependencies": dependencies,
        "covers_requires": {
            "build": list(plan["requires"]["build"]),
            "runtime": list(plan["requires"]["runtime"]),
        },
        "missing": [],
        "abi_conflicts": [],
        "packages": packages,
        "indexes": {
            url: {
                "baseurl": url,
                "revision": f"rev-{arch}",
                "primary_href": "repodata/primary.xml.gz",
                "primary_sha256": fixture_hash("primary", arch),
                "primary_sha256_declared": fixture_hash("primary", arch),
                "primary_bytes": 4321,
            }
            for url in plan["repos"]["published_index"]
        },
        "artifacts": artifacts,
    }


def upgrade_local_control(report):
    """Synthetic native observations for local controls; no signed RPM claim."""
    arch=report['arch']
    report['producer']['run_attempt']=1
    report['platform_proof']['baseline_probe']={
        'baseline':BASELINES[arch],'elf_machine':62 if arch=='x86_64' else 183,
        'execution_rc':0,'source_sha256':'1'*64,'executable_sha256':'2'*64}
    report['signatures']['fingerprint']=FACTORY_FPR
    for repo in report['repo_config']['repos']:
        repo['baseurl']=[repo['baseurl']];repo['gpgkey']=['file:///fixture-key']
    for state in report['signatures']['repomd_asc'].values():state['signer_fingerprint']=FACTORY_FPR
    report['artifacts']=[a for a in report['artifacts'] if a['kind']!='provider-rpm']
    for requirement,entries in report['dependencies'].items():
        entry=entries[0];selected=entry['selected'];abi=entry['abi']
        fingerprint=FACTORY_FPR if selected['repoid'].startswith('tunaos-published-') else DISTRO_FPR
        selected['signature'].update(fingerprint=fingerprint,cryptographic_signature_verified=True,digests_verified=True)
        entry['index_signature']={'verified':True,'signer_fingerprint':fingerprint}
        if 'index_provenance' not in entry:
            url=selected['repourl'];h=fixture_hash('systemprimary',url)
            entry['index_provenance']={'baseurl':url,'revision':'control-revision','primary_href':'repodata/primary.xml.gz',
                                      'primary_sha256':h,'primary_sha256_declared':h,'primary_bytes':1000}
        entry['index_provenance']['primary_checksum_algorithm']='sha256'
        entry['index_provenance']['primary_checksum_declared']=entry['index_provenance']['primary_sha256_declared']
        abi['files_count']=40;abi['dlopen']={name:True for name in abi['sonames']}
        paths=abi['headers'] or ['/usr/share/fixture/data']
        abi['file_ownership']={name:{'readable':True,'owner_nevra':selected['nevra'],'sha256':fixture_hash(name)} for name in paths}
        if abi['oracle']!='named-package':
            cap=requirement.split(' >=')[0];module=cap[len('pkgconfig('):-1]
            pc='/usr/lib64/pkgconfig/'+module+'.pc';abi['pc_file']=pc
            abi['file_ownership'][pc]={'readable':True,'owner_nevra':selected['nevra'],'sha256':fixture_hash(pc)}
            abi['sdk_compile_and_run']=True
        else:abi['smoke_required']=False
        if abi['oracle']=='grounded:gtk4-layer-shell':
            abi['library_version']=[1,3,0];abi['symbols_resolved']=True
            abi['symbols_sample']=['gtk_layer_init_for_window','gtk_layer_set_keyboard_mode','gtk_layer_set_namespace',
                'gtk_session_lock_instance_new','gtk_session_lock_instance_lock','gtk_session_lock_instance_unlock',
                'gtk_session_lock_instance_assign_window_to_monitor','gtk_session_lock_instance_is_locked']
            abi['headers_present']={path:True for path in ['/usr/include/gtk4-layer-shell/gtk4-layer-shell.h','/usr/include/gtk4-layer-shell/gtk4-session-lock.h']}
            abi['typelibs_present']={path:True for path in ['/usr/lib64/girepository-1.0/Gtk4LayerShell-1.0.typelib','/usr/lib64/girepository-1.0/Gtk4SessionLock-1.0.typelib']}
            abi['license_present']=True;abi['license_path']='/usr/share/licenses/gtk4-layer-shell/LICENSE'
            abi['file_ownership'][abi['license_path']]={'readable':True,'owner_nevra':selected['nevra'],'sha256':fixture_hash(abi['license_path'])}
            for path in [*abi['headers_present'],*abi['typelibs_present']]:
                abi['file_ownership'][path]={'readable':True,'owner_nevra':selected['nevra'],'sha256':fixture_hash(path)}
        if abi['oracle']=='grounded:libseat':abi['seatd_smoke_rc']=0
        for kind in ('provider-origin','provider-signature'):
            report['artifacts'].append({'name':'evidence/'+fixture_hash(requirement,kind)+'.json','kind':kind,
                                       'sha256':fixture_hash(requirement,kind),'run_id':RUN_ID,'require':requirement})
        report['artifacts'].append({'name':'evidence/'+fixture_hash(requirement)+'.rpm','kind':'provider-rpm',
            'sha256':selected['rpm_sha256'],'run_id':RUN_ID,'require':requirement,'repoid':selected['repoid'],
            'nevra':selected['nevra']})
    for provenance in report['indexes'].values():
        provenance['primary_checksum_algorithm']='sha256'
        provenance['primary_checksum_declared']=provenance['primary_sha256_declared']
    for state in report['signatures']['rpm_K']:
        selected=next(entry[0]['selected'] for entry in report['dependencies'].values() if entry[0]['selected']['nevra']==state['nevra'])
        state['fingerprint']=selected['signature']['fingerprint']


def bundle(arch="x86_64"):
    receipt = make_receipt()
    plan = make_plan(receipt, arch)
    report = make_report(plan, receipt, arch)
    upgrade_local_control(report)
    return report, plan, receipt, make_policy()


def clone(document):
    return json.loads(json.dumps(document))


def assert_rejects(func, *args, substr):
    try:
        func(*args)
    except ValueError as error:
        assert substr in str(error), f"want {substr!r} in {error}"
    else:
        raise AssertionError(f"admission accepted; want rejection containing {substr!r}")


def test_positive_full_33_require_observation():
    report, plan, receipt, policy = bundle("x86_64")
    assert len(plan["requires"]["build"]) == 24, "plan must carry 24 build requires"
    assert len(plan["requires"]["runtime"]) == 9, "plan must carry 9 runtime requires"
    summary = closure.validate_producer_evidence(report, plan, receipt, policy)
    assert summary["cell"] == "skipjack-el10-x86_64"
    assert summary["requires_observed"] == 33
    assert summary["run_id"] == RUN_ID
    assert summary["head_sha"] == HEAD
    assert len(summary["providers"]) == 33


def test_positive_aarch64_single_index():
    report, plan, receipt, policy = bundle("aarch64")
    assert plan["repos"]["published_index"] == INDEXES["aarch64"]
    summary = closure.validate_producer_evidence(report, plan, receipt, policy)
    assert summary["cell"] == "skipjack-el10-aarch64"
    assert summary["requires_observed"] == 33
    assert summary["indexes"] == INDEXES["aarch64"]


def test_rejects_forged_plan():
    report, plan, receipt, policy = bundle("x86_64")
    forged = clone(plan)
    forged["requires"] = {"build": ["invented-build"], "runtime": ["invented-runtime"]}
    forged_report = clone(report)
    forged_report["plan_sha256"] = closure.canonical_sha256(forged)
    forged_report["covers_requires"] = clone(forged["requires"])
    forged_report["producer"]["action_key"] = closure.compute_action_key(
        forged["cell"], forged["arch"], forged_report["base_image"], HEAD, WORKFLOW,
        forged_report["plan_sha256"], forged_report["receipt_sha256"],
    )
    assert_rejects(
        closure.validate_producer_evidence, forged_report, forged, receipt, policy,
        substr="staged cell require sets",
    )


def test_rejects_echo_only_partial_observations():
    report, plan, receipt, policy = bundle("x86_64")
    echo = clone(report)
    first = plan["requires"]["build"][0]
    echo["dependencies"] = {first: echo["dependencies"][first]}
    echo["packages"] = [p for p in echo["packages"] if p["require"] == first]
    assert echo["covers_requires"] == plan["requires"]
    assert_rejects(
        closure.validate_producer_evidence, echo, plan, receipt, policy, substr="no observations"
    )


def test_rejects_wrong_run_suffix():
    report, plan, receipt, policy = bundle("x86_64")
    bad = clone(report)
    bad["producer"]["run_url"] = (
        "https://github.com/tuna-os/tunaos-packages/actions/runs/99999"
    )
    assert closure.validate_closure_report(bad, plan["cell"]) == 33
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy, substr="run_url suffix"
    )


def test_rejects_arbitrary_signatures():
    report, plan, receipt, policy = bundle("x86_64")
    bad = clone(report)
    bad["signatures"]["keyid"] = "DEADBEEF"
    assert closure.validate_closure_report(bad, plan["cell"]) == 33
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy, substr="untrusted key"
    )
    bad = clone(report)
    require = plan["requires"]["build"][0]
    bad["dependencies"][require][0]["selected"]["signature"]["keyid"] = "DEADBEEF"
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="untrusted key",
    )


def test_rejects_cross_arch_supply():
    report, plan, receipt, policy = bundle("x86_64")
    # A provider whose arch disagrees with the cell.
    bad = clone(report)
    entry = bad["dependencies"]["dbus"][0]
    entry["selected"]["arch"] = "aarch64"
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy, substr="is not x86_64"
    )
    # A factory provider pinned to the sibling arch's index.
    bad = clone(report)
    entry = bad["dependencies"]["pkgconfig(gtk4-layer-shell-0) >= 1.1"][0]
    entry["selected"]["repourl"] = INDEXES["aarch64"][0]
    entry["served_from"]["repourl"] = INDEXES["aarch64"][0]
    entry["index_provenance"]["baseurl"] = INDEXES["aarch64"][0]
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="outside the plan indexes",
    )
    # A plan that itself carries the sibling arch's index.
    bad_plan = clone(plan)
    bad_plan["repos"]["published_index"] = list(INDEXES["x86_64"]) + INDEXES["aarch64"]
    bad = clone(report)
    bad["plan_sha256"] = closure.canonical_sha256(bad_plan)
    bad["producer"]["action_key"] = closure.compute_action_key(
        bad_plan["cell"], bad_plan["arch"], bad["base_image"], HEAD, WORKFLOW,
        bad["plan_sha256"], bad["receipt_sha256"],
    )
    assert_rejects(
        closure.validate_producer_evidence, bad, bad_plan, receipt, policy,
        substr="trusted x86_64 set",
    )


def test_rejects_stale_source():
    report, plan, receipt, policy = bundle("x86_64")
    bad = clone(report)
    bad["source_sha"] = "d" * 40
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="not the staged source",
    )
    bad = clone(report)
    bad["receipt_sha256"] = "0" * 64
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="receipt binding",
    )


def test_rejects_emulated_platform():
    report, plan, receipt, policy = bundle("aarch64")
    bad = clone(report)
    bad["platform_proof"]["runner_machine"] = "x86_64"
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy, substr="cross claim"
    )
    bad = clone(report)
    bad["platform_proof"]["container_machine"] = "x86_64"
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy, substr="want native"
    )
    bad = clone(report)
    bad["platform_proof"]["image_arch"] = "amd64"
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="does not match aarch64",
    )


def test_rejects_disabled_signature_checks():
    report, plan, receipt, policy = bundle("x86_64")
    bad = clone(report)
    bad["repo_config"]["gpgcheck"] = False
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="disabled signature",
    )
    bad = clone(report)
    bad["repo_config"]["repos"][0]["repo_gpgcheck"] = False
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="disabled metadata",
    )


def test_rejects_primary_divergence_and_unverified_repomd():
    report, plan, receipt, policy = bundle("x86_64")
    bad = clone(report)
    entry = bad["dependencies"]["pkgconfig(gtk4-layer-shell-0) >= 1.1"][0]
    entry["index_provenance"]["primary_sha256"] = "0" * 64
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="differ from repomd",
    )
    bad = clone(report)
    url = plan["repos"]["published_index"][0]
    bad["signatures"]["repomd_asc"][url]["verified"] = False
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="unverified",
    )


def test_rejects_untrusted_head_and_workflow():
    report, plan, receipt, policy = bundle("x86_64")
    bad = clone(report)
    bad["producer"]["head_sha"] = "e" * 40
    bad["producer"]["action_key"] = closure.compute_action_key(
        plan["cell"], plan["arch"], bad["base_image"], "e" * 40, WORKFLOW,
        bad["plan_sha256"], bad["receipt_sha256"],
    )
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="not a trusted head",
    )
    bad = clone(report)
    bad["producer"]["workflow"] = "someone-else.yml"
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="not the trusted producer",
    )


def test_rejects_unavailable_oracle():
    report, plan, receipt, policy = bundle("x86_64")
    bad = clone(report)
    bad["dependencies"]["dbus"][0]["abi"] = {"oracle": "unavailable"}
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy, substr="wrong capability ABI oracle"
    )


def test_rejects_versioned_constraint_failure():
    # "4.9" is lexicographically greater than "4.12" but numerically
    # older: admission must judge with the RPM comparator, not strings.
    report, plan, receipt, policy = bundle("x86_64")
    bad = clone(report)
    entry = bad["dependencies"]["pkgconfig(gtk4) >= 4.12"][0]
    entry["abi"]["pkgconfig_version"] = "4.9"
    entry["constraint"]["observed"] = "4.9"
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="fails the constraint",
    )


def test_rejects_missing_and_misbound_artifacts():
    report, plan, receipt, policy = bundle("x86_64")
    bad = clone(report)
    bad["artifacts"] = [a for a in bad["artifacts"] if a["kind"] != "repomd-asc"]
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="lacks a bound repomd-asc",
    )
    bad = clone(report)
    bad["artifacts"][0]["run_id"] = 777
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="from another run",
    )
    bad = clone(report)
    bad["artifacts"] = [a for a in bad["artifacts"] if a["kind"] != "provider-rpm"]
    assert_rejects(
        closure.validate_producer_evidence, bad, plan, receipt, policy,
        substr="lacks exact provider RPM bytes",
    )


def test_rejects_bad_policy():
    report, plan, receipt, _ = bundle("x86_64")
    bad = make_policy()
    bad["trusted_heads"] = []
    assert_rejects(
        closure.validate_producer_evidence, report, plan, receipt, bad, substr="trusted_heads"
    )
    bad = make_policy()
    bad["expected_indexes"] = {"x86_64": ["https://evil.example/"]}
    assert_rejects(
        closure.validate_producer_evidence, report, plan, receipt, bad,
        substr="not the trusted x86_64 set",
    )


def test_stub_still_always_fails():
    # The merged no-policy scaffolding keeps its semantics exactly:
    # even the fully-observed positive is rejected without trust roots.
    report, plan, receipt, _ = bundle("x86_64")
    assert_rejects(closure.admit_measured_closure, report, plan, receipt,
                   substr="not implemented")


def test_action_key_recomputes_and_detects_tamper():
    report, plan, receipt, _ = bundle("x86_64")
    key = report["producer"]["action_key"]
    assert key == closure.compute_action_key(
        plan["cell"], plan["arch"], report["base_image"], HEAD, WORKFLOW,
        report["plan_sha256"], report["receipt_sha256"],
    )
    tampered = closure.compute_action_key(
        plan["cell"], plan["arch"], report["base_image"], HEAD, WORKFLOW,
        "0" * 64, report["receipt_sha256"],
    )
    assert tampered != key


def test_producer_pure_helpers():
    section = producer.build_repo_section(0, INDEXES["x86_64"][0], "/etc/pki/rpm-gpg/TUNAOS-EL10")
    assert "gpgcheck=1" in section and "repo_gpgcheck=1" in section
    assert "priority=999" in section
    assert "excludepkgs=glib2 glib2-devel glib2-static" in section
    preamble = producer.dnf_preamble(["crb", "epel"])
    assert preamble[0] == ["dnf", "-qy", "install", "epel-release"]
    assert ["dnf", "config-manager", "--set-enabled", "crb"] in preamble
    assert preamble[-1] == ["dnf", "-qy", "makecache"]
    row = producer.parse_pipe_row(
            "gtk4-layer-shell-devel|0|1.3.0|1.el10|x86_64|tunaos-published-0|"
            "gtk4-layer-shell-1.3.0-1.el10.src.rpm|" + "ab" * 32, with_repoid=True)
    assert row["nevra"] == "gtk4-layer-shell-devel-1.3.0-1.el10.x86_64"
    assert row["evr"] == "0:1.3.0-1.el10"
    assert producer.parse_sigpgp_keyid("RSA/SHA256, Key ID 98c78b31") == "98C78B31"
    assert producer.rpm_k_state(0, "x.rpm: digests signatures OK") == "gpg-ok"
    assert producer.rpm_k_state(0, "x.rpm: digests signatures NOT OK") == "unverified"
    assert producer.rpm_k_state(1, "") == "unverified"
    assert producer.split_pkgconfig("pkgconfig(gtk4) >= 4.12") == ("gtk4", ">=", "4.12")
    assert producer.split_pkgconfig("dbus") == (None, None, None)
    assert producer.split_pkgconfig("pkgconfig(libseat)") == ("libseat", None, None)


def test_producer_decodes_factory_keyring():
    # Cross-checks the stdlib OpenPGP parser against the gpg-decoded
    # ground truth for public.gpg (blob 275f4077).
    keys = producer.parse_openpgp_keyids((ROOT / "public.gpg").read_bytes())
    assert keys[0]["keyid"] == FACTORY_PRIMARY
    assert keys[0]["fingerprint"] == "4E5CC9F8B3B521793D95266E629BE6EA45188366"
    assert keys[1]["keyid"] == FACTORY_SUBKEY
    assert keys[1]["fingerprint"] == FACTORY_FPR


def test_no_signature_bypass_in_producer_sources():
    # The forbidden literals are built dynamically so this file itself
    # never carries them either.
    forbidden = ["gpgcheck" + "=" + "0", "no" + "-" + "gpgcheck", "skip" + "-" + "verify"]
    for rel in ("scripts/el10-measure-closure.py", "scripts/el10-rpm-closure.py",
                ".github/workflows/el10-closure-producer.yml"):
        text = (ROOT / rel).read_text()
        for literal in forbidden:
            assert literal not in text, f"{rel} carries {literal!r}"


def test_measure_refuses_arch_mismatch_offline():
    import pathlib as _pathlib
    import types

    _, plan, receipt, _ = bundle("x86_64")
    args = types.SimpleNamespace(
        plan="plan.json", receipt="receipt.json", arch="aarch64", out="/nonexistent",
    )
    # Feed plan/receipt without touching disk or spawning anything: the
    # arch check raises before the first subprocess.
    original = _pathlib.Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if str(self).endswith("plan.json"):
            return json.dumps(plan)
        return json.dumps(receipt)

    _pathlib.Path.read_text = fake_read_text
    try:
        try:
            producer.cmd_measure(args)
        except ValueError as error:
            assert "arch mismatch" in str(error), error
        else:
            raise AssertionError("measure accepted mismatched arches")
    finally:
        _pathlib.Path.read_text = original



def test_actual_admission_requires_original_custody_even_for_complete_local_control():
    report,plan,receipt,policy=bundle()
    assert_rejects(closure.admit_producer_closure,report,plan,receipt,policy,
                   substr="independent original producer custody required")


def test_failed_abi_verdicts_and_wrong_oracle_cannot_pass_complete_control():
    report,plan,receipt,policy=bundle()
    requirement="pkgconfig(gtk4-layer-shell-0) >= 1.1"
    for field,value,reason in [
        ("symbols_resolved",False,"symbols"), ("sdk_compile_and_run",False,"compile/run"),
        ("dlopen",{"libgtk4-layer-shell.so.0":False},"SONAME"),
        ("library_version",[1,2,0],"version differs"),
        ("oracle","named-package","wrong capability ABI oracle"),
        ("license_present",False,"license missing"),
        ("file_ownership",{},"owner/readability")]:
        bad=clone(report);bad['dependencies'][requirement][0]['abi'][field]=value
        assert_rejects(closure.validate_producer_evidence,bad,plan,receipt,policy,substr=reason)
    for field in ('headers_present','typelibs_present'):
        bad=clone(report);values=bad['dependencies'][requirement][0]['abi'][field]
        values[next(iter(values))]=False
        assert_rejects(closure.validate_producer_evidence,bad,plan,receipt,policy,
                       substr="grounded GTK payload")


def test_report_keyring_cannot_authorize_arbitrary_published_provider():
    report,plan,receipt,policy=bundle()
    key='DEADBEEF'*5
    bad=clone(report);bad['rpm_keyring'].append({'keyid':key,'fingerprint':key})
    selected=bad['dependencies']['pkgconfig(gtk4-layer-shell-0) >= 1.1'][0]['selected']
    selected['signature'].update(fingerprint=key,keyid=key[-16:])
    for state in bad['signatures']['rpm_K']:
        if state['nevra']==selected['nevra']:state.update(fingerprint=key,keyid=key[-16:])
    assert_rejects(closure.validate_producer_evidence,bad,plan,receipt,policy,
                   substr="untrusted fingerprint")


def test_native_noarch_payload_positive_and_wrong_arch_negative():
    report,plan,receipt,policy=bundle()
    entry=report['dependencies']['sound-theme-freedesktop'][0]
    old_nevra=entry['selected']['nevra'];new_nevra=old_nevra.rsplit('.',1)[0]+'.noarch'
    entry['selected'].update(arch='noarch',nevra=new_nevra)
    entry['providers']=[new_nevra]
    for state in report['signatures']['rpm_K']:
        if state['nevra']==old_nevra:state['nevra']=new_nevra
    for artifact in report['artifacts']:
        if artifact.get('require')=='sound-theme-freedesktop' and artifact.get('kind')=='provider-rpm':
            artifact['nevra']=new_nevra
    entry['abi'].update(architecture_neutral_payload=True,noarch_regular_files_checked=8)
    assert closure.validate_producer_evidence(report,plan,receipt,policy)['requires_observed']==33
    bad=clone(report);bad['dependencies']['sound-theme-freedesktop'][0]['abi']['architecture_neutral_payload']=False
    assert_rejects(closure.validate_producer_evidence,bad,plan,receipt,policy,substr="noarch")
    bad=clone(report);bad['dependencies']['sound-theme-freedesktop'][0]['selected']['arch']='aarch64'
    assert_rejects(closure.validate_producer_evidence,bad,plan,receipt,policy,substr="not x86_64")

def main():
    tests = sorted(
        (name, func) for name, func in globals().items()
        if name.startswith("test_") and callable(func)
    )
    failed = 0
    for name, func in tests:
        try:
            func()
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok {name}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
