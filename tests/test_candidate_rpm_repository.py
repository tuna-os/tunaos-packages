"""Candidate integrity regressions at real process/filesystem boundaries.

INCIDENT-Alma-bootstrap: unsigned intermediate RPMs cannot satisfy a strict
signed local repository. Falsification: digest-only or failed signatures must
never become candidate supply, even when some other digest reports OK.
"""
from __future__ import annotations

import configparser
import fcntl
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/candidate-rpm-repository.py'


@pytest.fixture
def candidate(tmp_path):
    binq = tmp_path / 'bin'
    binq.mkdir()
    fake = binq / 'boundary'
    fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['BOUNDARY_LOG'], 'a') as stream: stream.write(json.dumps([name,args])+'\\n')
def option(flag): return args[args.index(flag)+1]
if name=='gpg':
    if '--list-secret-keys' in args: print('fpr:::::::::0123456789ABCDEF0123456789ABCDEF01234567:')
    elif '--export' in args: print('CANDIDATE PUBLIC KEY')
    elif '--detach-sign' in args:
        if os.environ.get('SIGNER_FAILURE'): sys.exit(2)
        if not pathlib.Path(args[-1]).is_file(): sys.exit(2)
        pathlib.Path(option('--output')).write_text('CANDIDATE SIGNATURE')
    elif '--verify' in args and os.environ.get('BAD_METADATA'): sys.exit(2)
elif name=='rpmsign':
    p=pathlib.Path(args[-1]); p.write_bytes(p.read_bytes()+b' SIGNED')
elif name=='rpmkeys':
    mode=os.environ.get('SIGNATURE_MODE','valid')
    if mode=='tampered': print('RSA Signature: BAD'); sys.exit(1)
    if mode=='digest_only': print('Payload SHA256 digest: OK')
    elif mode=='bad_with_digest_ok': print('RSA Signature: NOT OK\\nPayload SHA256 digest: OK')
    else: print('Header V4 RSA/SHA256 Signature, key ID 01234567: OK')
elif name=='createrepo_c':
    p=pathlib.Path(option('--outputdir'))/'repodata';p.mkdir(exist_ok=True)
    if not os.environ.get('MISSING_METADATA'): (p/'repomd.xml').write_text('<repomd/>')
elif name=='rpm':
    if '-qa' in args: print('native-package\\t0:1-1.el10.alma\\tx86_64')
    elif '--eval' in args: print('-O2 -march=x86-64-v2\\nx86_64\\n.el10.alma')
elif name=='gcc': print('gcc (Alma buildroot) 14.2.1')
''')
    fake.chmod(0o755)
    for tool in ['gpg', 'rpmsign', 'rpm', 'rpmkeys', 'createrepo_c', 'gcc']:
        (binq / tool).symlink_to(fake)
    log = tmp_path / 'boundary.log'
    log.write_text('')
    env = {**os.environ, 'PATH': str(binq) + ':' + os.environ['PATH'], 'BOUNDARY_LOG': str(log)}
    state, repo = tmp_path / 'state', tmp_path / 'repo'
    return state, repo, env, log


def invoke(candidate, operation, rpm=None, **changes):
    state, repo, env, _ = candidate
    command = [sys.executable, str(SCRIPT), operation, '--state', str(state), '--repo', str(repo)]
    if rpm is not None:
        command += ['--rpm', str(rpm)]
    return subprocess.run(command, env={**env, **changes}, capture_output=True, text=True)


def calls(candidate):
    return [json.loads(line) for line in candidate[3].read_text().splitlines()]


def bootstrap(candidate):
    result = invoke(candidate, 'init')
    assert result.returncode == 0, result.stderr


def test_initial_key_is_private_ephemeral_and_explicitly_unpromoted(candidate):
    bootstrap(candidate)
    state, _, _, _ = candidate
    identity = json.loads((state / 'identity.json').read_text())
    assert identity == {'scope': 'run-local-unpromoted-candidate', 'productionReady': False,
                        'fingerprint': '0123456789ABCDEF0123456789ABCDEF01234567'}
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / 'gnupg').stat().st_mode & 0o777 == 0o700
    generation = next(args for name, args in calls(candidate) if '--quick-generate-key' in args)
    assert generation[-1] == '1d'
    assert generation[generation.index('--homedir') + 1] == str(state / 'gnupg')


def test_restored_seed_is_rejected_without_signing_it(candidate):
    state, repo, _, _ = candidate
    repo.mkdir(); (repo / 'foreign.rpm').write_bytes(b'foreign')
    result = invoke(candidate, 'init')
    assert result.returncode != 0
    assert 'empty candidate repository' in result.stderr
    assert not state.exists()
    assert calls(candidate) == []


@pytest.mark.parametrize('location', ['same', 'nested'])
def test_private_state_cannot_be_published_under_repo(candidate, location):
    state, repo, env, log = candidate
    state = repo if location == 'same' else repo / 'private'
    result = invoke((state, repo, env, log), 'init')
    assert result.returncode != 0
    assert 'outside repository artifacts' in result.stderr
    assert not repo.exists()


def test_symlink_repository_is_not_a_bootstrap_destination(candidate, tmp_path):
    state, repo, env, log = candidate
    destination = tmp_path / 'unrelated'; destination.mkdir()
    repo.symlink_to(destination, target_is_directory=True)
    result = invoke(candidate, 'init')
    assert result.returncode != 0
    assert not (destination / 'repo.lock').exists()


def test_exact_staged_rpm_is_verified_with_only_candidate_key_before_replace(candidate, tmp_path):
    bootstrap(candidate)
    source = tmp_path / 'built.rpm'; source.write_bytes(b'original')
    result = invoke(candidate, 'install', source)
    assert result.returncode == 0, result.stderr
    state, repo, _, _ = candidate
    assert source.read_bytes() == b'original'
    assert (repo / source.name).read_bytes() == b'original SIGNED'
    records = calls(candidate)
    signing = next(args for name, args in records if name == 'rpmsign')
    verifying = next(args for name, args in records if name == 'rpmkeys')
    assert signing[-1] == verifying[-1]
    assert signing[-1] != str(source)
    imported = next(args for name, args in records if name == 'rpm' and '--import' in args)
    assert imported[-1] == str(state / 'keys/candidate-public.gpg')
    assert verifying[verifying.index('--dbpath') + 1] == imported[imported.index('--dbpath') + 1]
    assert not list(repo.glob('.candidate-*'))


@pytest.mark.parametrize('mode', ['tampered', 'digest_only', 'bad_with_digest_ok'])
def test_bad_signature_never_replaces_last_candidate(candidate, tmp_path, mode):
    bootstrap(candidate)
    repo = candidate[1]
    source = tmp_path / 'built.rpm'; source.write_bytes(b'new')
    (repo / source.name).write_bytes(b'last-good-candidate')
    result = invoke(candidate, 'install', source, SIGNATURE_MODE=mode)
    assert result.returncode != 0
    assert (repo / source.name).read_bytes() == b'last-good-candidate'
    assert not list(repo.glob('.candidate-*'))


def test_symlink_rpm_input_is_not_signed(candidate, tmp_path):
    bootstrap(candidate)
    source = tmp_path / 'original.rpm'; source.write_bytes(b'unrelated')
    link = tmp_path / 'link.rpm'; link.symlink_to(source)
    result = invoke(candidate, 'install', link)
    assert result.returncode != 0
    assert source.read_bytes() == b'unrelated'
    assert not any(name == 'rpmsign' for name, _ in calls(candidate))


def test_bad_candidate_stops_before_repository_indexing(candidate):
    bootstrap(candidate)
    (candidate[1] / 'bad.rpm').write_bytes(b'tampered')
    result = invoke(candidate, 'index', SIGNATURE_MODE='tampered')
    assert result.returncode != 0
    assert not any(name == 'createrepo_c' for name, _ in calls(candidate))
    assert not (candidate[1] / 'repodata').exists()


@pytest.mark.parametrize('condition', ['BAD_METADATA', 'MISSING_METADATA'])
def test_missing_or_bad_metadata_never_gets_final_signature(candidate, condition):
    bootstrap(candidate)
    result = invoke(candidate, 'index', **{condition: '1'})
    assert result.returncode != 0
    assert not (candidate[1] / 'repodata/repomd.xml.asc').exists()


def test_metadata_signature_is_verified_before_final_install(candidate):
    bootstrap(candidate)
    result = invoke(candidate, 'index')
    assert result.returncode == 0, result.stderr
    assert (candidate[1] / 'repodata/repomd.xml.asc').read_text() == 'CANDIDATE SIGNATURE'
    assert not (candidate[1] / 'repodata/repomd.xml.asc.new').exists()
    verify = next(args for name, args in calls(candidate) if name == 'gpg' and '--verify' in args)
    assert Path(verify[-1]).name == 'repomd.xml'
    assert Path(verify[-1]).parent.name == 'repodata'
    assert Path(verify[-1]).parent.parent.name.startswith('.candidate-index-')
    assert verify[-2] == verify[-1] + '.asc'


def test_repository_writer_waits_for_existing_reader(candidate):
    bootstrap(candidate)
    repo = candidate[1]
    with (repo / 'repo.lock').open() as reader:
        fcntl.flock(reader, fcntl.LOCK_SH)
        state, _, env, _ = candidate
        child = subprocess.Popen([sys.executable, str(SCRIPT), 'index', '--state', str(state),
                                  '--repo', str(repo)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.2)
            assert child.poll() is None
            assert not any(name == 'createrepo_c' for name, _ in calls(candidate))
            fcntl.flock(reader, fcntl.LOCK_UN)
            _, err = child.communicate(timeout=15)
            assert child.returncode == 0, err
        finally:
            if child.poll() is None:
                child.kill(); child.communicate()


def test_mock_policy_executes_chroot_bind_signed_metadata_and_no_cache(candidate):
    bootstrap(candidate)
    config = {'target_arch': 'x86_64', 'plugin_conf': {'root_cache_enable': True,
                             'bind_mount_opts': {'dirs': [('/keys', '/keys'), ('/local-repo', '/local-repo')]}},
              'dnf.conf': '[local-build]\ngpgcheck=1\nbaseurl=file:///local-repo/\n'}
    namespace = {'config_opts': config}
    policy = (candidate[0] / 'keys/mock-candidate-policy.cfg').read_text()
    exec(compile(policy, 'candidate-policy.cfg', 'exec'), namespace)
    assert config['plugin_conf']['root_cache_enable'] is False
    assert config['plugin_conf']['bind_mount_enable'] is True
    assert config['plugin_conf']['bind_mount_opts']['dirs'].count(('/keys', '/keys')) == 1
    # INCIDENT-bootstrap-key: Mock does not run bind_mount in bootstrap.
    # Falsification: a key outside the file:// repository mount cannot be read
    # by bootstrap DNF even if a copied plugin configuration names that path.
    assert 'bootstrap_plugin_conf' not in config
    assert (candidate[1] / 'candidate-public.gpg').read_bytes() == (candidate[0] / 'keys/candidate-public.gpg').read_bytes()
    ini = configparser.ConfigParser(); ini.read_string(config['dnf.conf'])
    assert ini['local-build']['gpgcheck'] == '1'
    assert ini['local-build']['repo_gpgcheck'] == '1'
    assert config['rpmbuild_command'] == '/usr/bin/python3 /keys/alma-rpmbuild-guard.py x86_64'
    assert (candidate[0] / 'keys/alma-rpmbuild-guard.py').read_bytes() == (ROOT / 'scripts/alma-rpmbuild-guard.py').read_bytes()


def test_observation_script_runs_inventory_macros_and_compiler_inside_its_environment(candidate):
    bootstrap(candidate)
    state, _, env, _ = candidate
    result = subprocess.run(['sh', str(state / 'keys/measure-buildroot.sh')], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'productionReady=false' in result.stdout
    assert 'native-package\t0:1-1.el10.alma\tx86_64' in result.stdout
    assert '-march=x86-64-v2' in result.stdout
    assert 'gcc (Alma buildroot) 14.2.1' in result.stdout
    assert any(name == 'rpm' and '-qa' in args for name, args in calls(candidate))
    assert any(name == 'rpm' and '--eval' in args for name, args in calls(candidate))


def test_alma_native_backend_fails_before_using_unbound_keys(tmp_path):
    manifest = tmp_path / 'build-order.yml'; manifest.write_text('target: almalinux-10\ntiers: []\n')
    result = subprocess.run(['bash', str(ROOT / 'scripts/build-chain.sh'), '--manifest', str(manifest),
                             '--mock-config', 'alma10-ci', '--backend', 'native', '--dry-run'],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'requires the podman mock backend' in result.stderr


@pytest.mark.parametrize('condition', ['BAD_METADATA', 'MISSING_METADATA', 'SIGNER_FAILURE'])
def test_failed_metadata_update_preserves_previous_signed_pair(candidate, condition):
    bootstrap(candidate)
    live = candidate[1] / 'repodata'; live.mkdir()
    (live / 'repomd.xml').write_bytes(b'<repomd>previous</repomd>')
    (live / 'repomd.xml.asc').write_bytes(b'previous-valid-signature')
    result = invoke(candidate, 'index', **{condition: '1'})
    assert result.returncode != 0
    assert (live / 'repomd.xml').read_bytes() == b'<repomd>previous</repomd>'
    assert (live / 'repomd.xml.asc').read_bytes() == b'previous-valid-signature'
    assert not list(candidate[1].glob('.candidate-index-*'))


def test_failed_filesystem_swap_restores_previous_signed_pair(candidate, monkeypatch):
    bootstrap(candidate)
    state, repo, env, _ = candidate
    live = repo / 'repodata'; live.mkdir()
    (live / 'repomd.xml').write_bytes(b'previous-metadata')
    (live / 'repomd.xml.asc').write_bytes(b'previous-signature')
    spec = importlib.util.spec_from_file_location('candidate_repository', SCRIPT)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    for key in ['PATH', 'BOUNDARY_LOG']:
        monkeypatch.setenv(key, env[key])
    real_replace = os.replace
    attempts = []
    def filesystem_replace(source, destination):
        source, destination = Path(source), Path(destination)
        attempts.append((source, destination))
        # Inject one filesystem syscall failure, retaining all real locks,
        # signing processes, staging paths and successful rename operations.
        if source.name == 'repodata' and source.parent.name.startswith('.candidate-index-'):
            raise OSError('injected filesystem rename failure')
        return real_replace(source, destination)
    monkeypatch.setattr(os, 'replace', filesystem_replace)
    with pytest.raises(OSError, match='filesystem rename failure'):
        module.index(repo, state)
    assert (live / 'repomd.xml').read_bytes() == b'previous-metadata'
    assert (live / 'repomd.xml.asc').read_bytes() == b'previous-signature'
    assert any(source.name == 'previous-repodata' and destination == live for source, destination in attempts)
    assert not list(repo.glob('.candidate-index-*'))


@pytest.fixture
def candidate_module():
    spec = importlib.util.spec_from_file_location('candidate_repository', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_admission_refuses_existing_artifacts_before_verification(candidate_module, tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    (repo / 'foreign.rpm').write_bytes(b'foreign')
    with pytest.raises(ValueError, match='empty candidate repository'):
        candidate_module.admit_snapshot(tmp_path / 'snapshot', tmp_path / 'manifest',
            tmp_path / 'identity', tmp_path / 'bundle', tmp_path / 'api', repo, tmp_path / 'state')
    assert (repo / 'foreign.rpm').read_bytes() == b'foreign'


def test_admission_requires_fresh_initialized_key_state(candidate_module, tmp_path):
    with pytest.raises(ValueError, match='fresh candidate state'):
        candidate_module.admit_snapshot(tmp_path / 'snapshot', tmp_path / 'manifest',
            tmp_path / 'identity', tmp_path / 'bundle', tmp_path / 'api', tmp_path / 'repo', tmp_path / 'state')


@pytest.mark.parametrize('raw', [b'', b'not an RPM', bytes.fromhex('edabeedb') + bytes(92),
    bytes.fromhex('edabeedb') + bytes(92) + bytes.fromhex('8eade80100000000ffffffffffffffff')])
def test_payload_identity_rejects_malformed_rpm(candidate_module, tmp_path, raw):
    rpm = tmp_path / 'malformed.rpm'; rpm.write_bytes(raw)
    with pytest.raises(ValueError): candidate_module.rpm_content(rpm)


def test_payload_identity_hashes_real_header_and_payload_not_signature(candidate_module, tmp_path, monkeypatch):
    import hashlib
    import struct
    import types
    lead = bytes.fromhex('edabeedb') + bytes(92)
    header = bytes.fromhex('8eade80100000000') + struct.pack('>II', 0, 3) + b'hdr'
    def rpm_bytes(signature):
        signature_header = bytes.fromhex('8eade80100000000') + struct.pack('>II', 0, len(signature)) + signature
        padding = bytes((-len(signature_header)) % 8)
        return lead + signature_header + padding + header + b'payload'
    first = tmp_path / 'old.rpm'; first.write_bytes(rpm_bytes(b'old signature'))
    second = tmp_path / 'new.rpm'; second.write_bytes(rpm_bytes(b'new longer signature'))
    calls = []
    def rpm_query(command, **kwargs):
        calls.append(command)
        return types.SimpleNamespace(stdout='native\t0:1-1.el10.alma\tx86_64\n')
    monkeypatch.setattr(candidate_module.subprocess, 'run', rpm_query)
    expected = {'nevra': 'native\t0:1-1.el10.alma\tx86_64',
                'headerPayloadDigest': 'sha256:' + hashlib.sha256(header + b'payload').hexdigest(),
                'payloadDigest': 'sha256:' + hashlib.sha256(b'payload').hexdigest()}
    assert candidate_module.rpm_content(first) == expected
    assert candidate_module.rpm_content(second) == expected
    second.write_bytes(rpm_bytes(b'new longer signature') + b'changed')
    assert candidate_module.rpm_content(second) != expected
    assert all(command[0:2] == ['rpm', '-qp'] for command in calls)


def test_admission_authenticator_failure_leaves_empty_candidate_unchanged(candidate_module, tmp_path, monkeypatch):
    repo = tmp_path / 'repo'; repo.mkdir()
    (repo / 'repodata').mkdir(); (repo / 'repodata/repomd.xml').write_text('old empty metadata')
    state = tmp_path / 'state'; state.mkdir(); (state / 'identity.json').write_text('{}')
    manifest = tmp_path / 'snapshot.json'; manifest.write_text('{}')
    calls = []
    def failed_verifier(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(candidate_module.subprocess, 'run', failed_verifier)
    with pytest.raises(subprocess.CalledProcessError):
        candidate_module.admit_snapshot(tmp_path / 'snapshot', manifest, tmp_path / 'identity',
            tmp_path / 'bundle', tmp_path / 'api', repo, state)
    assert len(calls) == 1
    assert calls[0][1:3] == [str(SCRIPT.with_name('alma-candidate-snapshot.py')), 'verify']
    assert (repo / 'repodata/repomd.xml').read_text() == 'old empty metadata'
    assert not (repo / 'admission-receipt.json').exists()


def test_admission_receipt_prevents_double_admission(candidate_module, tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    (repo / 'admission-receipt.json').write_text('{"productionReady":false}')
    with pytest.raises(ValueError, match='empty candidate repository'):
        candidate_module.admit_snapshot(tmp_path / 'snapshot', tmp_path / 'manifest',
            tmp_path / 'identity', tmp_path / 'bundle', tmp_path / 'api', repo, tmp_path / 'state')
