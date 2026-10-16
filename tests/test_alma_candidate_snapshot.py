"""Alma partials require complete inventories and externally verified producers.

Fixtures exercise the gh result/API boundaries; they do not prove live signatures.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/alma-candidate-snapshot.py'
spec = importlib.util.spec_from_file_location('alma_snapshot', SCRIPT)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / 'staged'; root.mkdir()
    (root / 'candidate-public.gpg').write_bytes(b'public key')
    (root / 'native.rpm').write_bytes(b'RPM bytes')
    binding = {'repository': 'tuna-os/tunaos-packages', 'sourceRevision': 'a' * 40,
               'workflow': '.github/workflows/package-factory.yml',
               'signerWorkflow': '.github/workflows/package-factory-cell.yml', 'sourceRef': 'refs/heads/main',
               'runId': 123, 'runAttempt': 2, 'cell': 'gnome50-alma10-x86_64',
               'actionKey': 'sha256:' + 'b' * 64, 'target': 'alma10',
               'platform': {'os': 'linux', 'architecture': 'amd64', 'variant': 'v2'},
               'cpuBaseline': 'x86-64-v2', 'baseDigest': 'sha256:' + 'c' * 64}
    completed = [{'name': 'native', 'inputDigest': 'sha256:' + 'd' * 64,
                  'outputs': [{'path': 'native.rpm', 'digest': 'sha256:' + hashlib.sha256(b'RPM bytes').hexdigest()}]}]
    document = snapshot.create(root, binding, 'candidate-public.gpg', 'A' * 40, completed)
    raw = json.dumps(document).encode()
    uri = 'https://github.com/tuna-os/tunaos-packages'
    certificate = {'issuer': 'https://token.actions.githubusercontent.com', 'sourceRepositoryURI': uri,
                   'sourceRepositoryDigest': 'a' * 40, 'sourceRepositoryRef': 'refs/heads/main',
                   'buildSignerURI': uri + '/.github/workflows/package-factory-cell.yml@refs/heads/main',
                   'buildSignerDigest': 'a' * 40, 'runInvocationURI': uri + '/actions/runs/123/attempts/2'}
    verified = [{'verificationResult': {'signature': {'certificate': certificate},
                 'statement': {'predicateType': 'https://slsa.dev/provenance/v1',
                               'subject': [{'name': 'snapshot.json', 'digest': {'sha256': hashlib.sha256(raw).hexdigest()}}]}}}]
    api = {'id': 123, 'run_attempt': 2, 'head_sha': 'a' * 40,
           'path': '.github/workflows/package-factory.yml',
           'head_repository': {'full_name': 'tuna-os/tunaos-packages'},
           'repository': {'full_name': 'tuna-os/tunaos-packages'}}
    return root, document, raw, binding, verified, api


def test_complete_sorted_inventory_remains_unpromoted(candidate):
    root, document, raw, binding, verified, api = candidate
    assert [entry['path'] for entry in document['inventory']] == ['candidate-public.gpg', 'native.rpm']
    assert snapshot.validate(root, document, raw, binding, verified, api)['productionReady'] is False
    assert document['chainComplete'] is False


@pytest.mark.parametrize('change', ['extra', 'missing', 'tamper', 'symlink', 'hardlink', 'directory_link', 'private'])
def test_staged_inventory_mutations_are_rejected(candidate, change, tmp_path):
    root, document, raw, binding, verified, api = candidate
    rpm = root / 'native.rpm'
    if change == 'extra': (root / 'extra').write_text('unexpected')
    elif change == 'missing': rpm.unlink()
    elif change == 'tamper': rpm.write_bytes(b'tampered')
    elif change == 'symlink': rpm.unlink(); rpm.symlink_to(tmp_path / 'absent')
    elif change == 'hardlink': os.link(rpm, tmp_path / 'linked')
    elif change == 'directory_link': (root / 'linked').symlink_to(tmp_path, target_is_directory=True)
    elif change == 'private': (root / 'gnupg').mkdir()
    with pytest.raises(ValueError): snapshot.validate(root, document, raw, binding, verified, api)


@pytest.mark.parametrize('field,value', [('runId', True), ('runAttempt', 2.0), ('sourceRevision', 'short'),
    ('actionKey', 'b' * 64), ('baseDigest', 'latest'), ('target', 'el10'), ('cpuBaseline', 'x86-64-v3'),
    ('workflow', '.github/workflows/../other.yml')])
def test_malformed_identity_is_rejected(candidate, field, value):
    binding = copy.deepcopy(candidate[3]); binding[field] = value
    with pytest.raises(ValueError): snapshot.identity(binding)


@pytest.mark.parametrize('field', ['repository', 'sourceRepositoryDigest', 'sourceRepositoryRef',
                                    'buildSignerURI', 'buildSignerDigest', 'runInvocationURI', 'issuer'])
def test_wrong_certificate_binding_is_rejected(candidate, field):
    root, document, raw, binding, verified, api = candidate
    certificate = verified[0]['verificationResult']['signature']['certificate']
    if field == 'repository': field = 'sourceRepositoryURI'
    certificate[field] = 'wrong'
    with pytest.raises(ValueError): snapshot.validate(root, document, raw, binding, verified, api)


@pytest.mark.parametrize('field,value', [('id', 124), ('id', True), ('run_attempt', 1),
    ('head_sha', 'b' * 40), ('path', '.github/workflows/other.yml'), ('repository', {'full_name': 'other/repo'})])
def test_independent_api_producer_mismatch_is_rejected(candidate, field, value):
    root, document, raw, binding, verified, api = candidate
    api[field] = value
    with pytest.raises(ValueError): snapshot.validate(root, document, raw, binding, verified, api)


@pytest.mark.parametrize('kind', ['claimed_boolean', 'empty', 'wrong_subject', 'missing_certificate', 'wrong_predicate'])
def test_claimed_or_unbound_authentication_is_rejected(candidate, kind):
    root, document, raw, binding, verified, api = candidate
    if kind == 'claimed_boolean': verified = {'authenticated': True}
    elif kind == 'empty': verified = []
    elif kind == 'wrong_subject': verified[0]['verificationResult']['statement']['subject'][0]['digest']['sha256'] = '0' * 64
    elif kind == 'missing_certificate': del verified[0]['verificationResult']['signature']
    elif kind == 'wrong_predicate': verified[0]['verificationResult']['statement']['predicateType'] = 'untrusted'
    with pytest.raises(ValueError): snapshot.validate(root, document, raw, binding, verified, api)


@pytest.mark.parametrize('kind', ['duplicate', 'unsafe', 'unsorted', 'readiness', 'missing_rpm', 'wrong_output', 'unknown'])
def test_manifest_shape_and_completion_mutations_are_rejected(candidate, kind):
    document = candidate[1]
    if kind == 'duplicate': document['inventory'].append(copy.deepcopy(document['inventory'][0]))
    elif kind == 'unsafe': document['inventory'][0]['path'] = '../key'
    elif kind == 'unsorted': document['inventory'].reverse()
    elif kind == 'readiness': document['productionReady'] = True
    elif kind == 'missing_rpm': document['completedPackages'] = []
    elif kind == 'wrong_output': document['completedPackages'][0]['outputs'][0]['digest'] = 'sha256:' + '0' * 64
    elif kind == 'unknown': document['authenticated'] = True
    with pytest.raises(ValueError): snapshot.validate_manifest(document)


@pytest.mark.parametrize('raw', ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_ambiguous_json_is_rejected(tmp_path, raw):
    path = tmp_path / 'bad.json'; path.write_text(raw)
    with pytest.raises(ValueError): snapshot.load(path)


def test_snapshot_object_cannot_differ_from_attested_bytes(candidate):
    root, document, raw, binding, verified, api = candidate
    document['chainComplete'] = True
    with pytest.raises(ValueError, match='attested snapshot bytes'):
        snapshot.validate(root, document, raw, binding, verified, api)


def test_valid_arm_identity_keeps_its_own_baseline(candidate):
    binding = copy.deepcopy(candidate[3])
    binding['platform'] = {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'}
    binding['cpuBaseline'] = 'armv8-a'
    snapshot.identity(binding)
    binding['cpuBaseline'] = 'x86-64-v2'
    with pytest.raises(ValueError): snapshot.identity(binding)


def test_cli_cryptographic_verifier_failure_cannot_be_a_claimed_success(candidate, tmp_path, monkeypatch):
    import subprocess
    import sys
    root, document, raw, binding, verified, api = candidate
    manifest = tmp_path / 'snapshot.json'; manifest.write_bytes(raw)
    identity_file = tmp_path / 'identity.json'; identity_file.write_text(json.dumps(binding))
    api_file = tmp_path / 'api.json'; api_file.write_text(json.dumps(api))
    bundle = tmp_path / 'bundle.json'; bundle.write_text('{"authenticated":true}')
    calls = []

    def failed_verifier(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(snapshot.subprocess, 'run', failed_verifier)
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), 'verify', '--root', str(root),
        '--identity', str(identity_file), '--manifest', str(manifest), '--bundle', str(bundle), '--api-run', str(api_file)])
    with pytest.raises(subprocess.CalledProcessError): snapshot.main()
    assert calls == [['gh', 'attestation', 'verify', str(manifest), '--hostname', 'github.com', '--repo', 'tuna-os/tunaos-packages',
        '--signer-workflow', 'tuna-os/tunaos-packages/.github/workflows/package-factory-cell.yml',
        '--signer-digest', 'a' * 40, '--source-digest', 'a' * 40, '--source-ref', 'refs/heads/main',
        '--bundle', str(bundle), '--format', 'json']]


def test_fork_producer_is_rejected(candidate):
    root, document, raw, binding, verified, api = candidate
    api['head_repository']['full_name'] = 'fork/packages'
    with pytest.raises(ValueError): snapshot.validate(root, document, raw, binding, verified, api)


def test_symlink_ancestor_root_is_rejected(candidate, tmp_path):
    link = tmp_path / 'alias'; link.symlink_to(candidate[0].parent, target_is_directory=True)
    with pytest.raises(ValueError): snapshot.inventory(link / candidate[0].name)


@pytest.mark.parametrize('name', ['secret.key', 'secret.pem', 'secret.p12', 'secret.pfx'])
def test_private_key_extensions_are_rejected(candidate, name):
    (candidate[0] / name).write_bytes(b'private')
    with pytest.raises(ValueError): snapshot.inventory(candidate[0], 'candidate-public.gpg')


def test_metadata_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, 'MAX_METADATA', 4)
    path = tmp_path / 'large.json'; path.write_text('{"a":1}')
    with pytest.raises(ValueError, match='size limit'): snapshot.load(path)


@pytest.mark.parametrize('limit', ['MAX_FILES', 'MAX_FILE', 'MAX_TOTAL'])
def test_inventory_limits_are_enforced(candidate, monkeypatch, limit):
    monkeypatch.setattr(snapshot, limit, 1)
    with pytest.raises(ValueError, match='size limit'):
        snapshot.inventory(candidate[0], 'candidate-public.gpg')


def test_arm_unspecified_variant_is_valid_but_stays_exact(candidate):
    binding = copy.deepcopy(candidate[3])
    binding['platform'] = {'os': 'linux', 'architecture': 'arm64', 'variant': None}
    binding['cpuBaseline'] = 'armv8-a'
    snapshot.identity(binding)
