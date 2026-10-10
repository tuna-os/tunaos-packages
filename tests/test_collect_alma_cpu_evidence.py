"""CI CPU collection must prove bytes, signatures and restricted execution.

Falsification: substitute an unknown mirror, package identity, signing key,
installed ELF, or an unrestricted CPU control; collection must fail closed.
These tests are authored for CI. Local build/test execution is not authorized.
"""
import importlib.util
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    'alma_collector', Path(__file__).parents[1] / 'scripts/collect-alma-cpu-evidence.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


@pytest.mark.parametrize('url', [
    'http://repo.almalinux.org/almalinux/10/BaseOS/x86_64_v2/os/',
    'https://evil.example/almalinux/10/BaseOS/x86_64_v2/os/',
    'https://user@repo.almalinux.org/almalinux/10/BaseOS/x86_64_v2/os/',
    'https://repo.almalinux.org/almalinux/10/BaseOS/x86_64/os/',
    'https://repo.almalinux.org/almalinux/10/BaseOS/x86_64_v2/os/?token=secret',
    'https://repo.almalinux.org/almalinux/10/../BaseOS/x86_64_v2/os/',
])
def test_repository_allowlist_rejects_unknown_or_unsafe_inputs(url):
    with pytest.raises(collector.CollectionError):
        collector.repository_url(url)


@pytest.mark.parametrize('arch', ['x86_64_v2', 'aarch64'])
def test_native_repo_architecture_is_explicit(arch):
    url = f'https://repo.almalinux.org/almalinux/10/BaseOS/{arch}/os/'
    assert collector.repository_url(url) == url


@pytest.mark.parametrize('path', ['../escape', '/absolute', '//evil.example/x',
                                 'https://evil.example/x', 'Packages/%2e%2e/x',
                                 'Packages/x?secret=yes', 'Packages\\x'])
def test_metadata_location_cannot_escape_approved_origin(path):
    with pytest.raises(collector.CollectionError):
        collector.relative_url('https://repo.almalinux.org/almalinux/10/', path)


def test_metadata_relative_path_preserves_approved_repo():
    assert collector.relative_url('https://repo.almalinux.org/almalinux/10/', 'Packages/glibc.rpm').endswith('/10/Packages/glibc.rpm')


@pytest.mark.parametrize('text', ['glibc\t0\t2.39\t1.el10\tx86_64_v2\nother\t0\t1\t1\tx86_64_v2\n',
                                 'glibc\t0\t2.39\t1.el10\n',
                                 'glibc;echo\t0\t2.39\t1.el10\tx86_64_v2\n'])
def test_installed_soname_provider_must_be_unique_native_identity(text):
    with pytest.raises(collector.CollectionError):
        collector.native_identity(text)


def test_native_version_identity_retains_epoch_release_and_vendor_arch():
    actual = collector.native_identity('glibc\t0\t2.39\t128.el10_2.alma.1\tx86_64_v2\n')
    assert actual == {'name': 'glibc', 'epoch': '0', 'version': '2.39',
                      'release': '128.el10_2.alma.1', 'architecture': 'x86_64_v2'}


def test_installed_elf_bytes_must_match_authenticated_rpm(tmp_path):
    path = tmp_path / 'usr/lib64/libc.so.6'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'authenticated')
    collector.bind_installed_elf(tmp_path, './usr/lib64/libc.so.6', b'authenticated')
    with pytest.raises(collector.CollectionError, match='byte-mismatch'):
        collector.bind_installed_elf(tmp_path, './usr/lib64/libc.so.6', b'tampered')


def test_installed_symlink_cannot_escape_consumer_root(tmp_path):
    root = tmp_path / 'consumer'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'ELF')
    (root / 'library').symlink_to(outside)
    with pytest.raises(collector.CollectionError):
        collector.bind_installed_elf(root, 'library', b'ELF')


@pytest.mark.parametrize('version', [True, '1', 2])
def test_compiler_observation_version_is_strict(version):
    with pytest.raises(collector.CollectionError):
        collector.compiler_flags({'schemaVersion': version, 'cpuBaseline': 'x86-64-v2',
                                  'flags': {'CFLAGS': '-march=x86-64-v2'}}, 'x86-64-v2')


@pytest.mark.parametrize('observation', [None, [], True])
def test_malformed_compiler_envelopes_raise_structured_collection_error(observation):
    with pytest.raises(collector.CollectionError):
        collector.compiler_flags(observation, 'x86-64-v2')


def test_compiler_observation_preserves_go_identity_and_link_flags():
    flags = collector.compiler_flags({'schemaVersion': 1, 'cpuBaseline': 'x86-64-v2',
                                      'flags': {'CFLAGS': '-march=x86-64-v2 -fstack-protector-strong',
                                                'GOAMD64': 'v2', 'LDFLAGS': '-Wl,-z,relro'}}, 'x86-64-v2')
    assert 'GOAMD64=v2' in flags
    assert '-fstack-protector-strong' in flags
    assert '-Wl,-z,relro' in flags


def test_signature_key_must_match_pinned_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, 'key_fingerprint', lambda key, directory: ('A' * 40, {}))
    with pytest.raises(collector.CollectionError, match='fingerprint-mismatch'):
        collector.verify_signature(tmp_path / 'rpm', tmp_path / 'key', collector.ALMA_FINGERPRINT, tmp_path)


@pytest.mark.parametrize('signature', [
    'Header SHA256 digest: OK\nPayload SHA256 digest: OK\n',
    'Header RSA Signature, key ID c2a1e572: NOKEY\nPayload SHA256 digest: OK\n',
    'RSA Signature, key ID deadbeef: OK\nPayload SHA256 digest: OK\n',
    'RSA Signature, key ID c2a1e572: OK\nPayload SHA256 digest: NOT OK\n',
])
def test_unsigned_or_wrongly_signed_native_rpm_is_rejected(tmp_path, monkeypatch, signature):
    monkeypatch.setattr(collector, 'key_fingerprint', lambda key, directory: (collector.ALMA_FINGERPRINT, {}))
    monkeypatch.setattr(collector, 'checked', lambda argv: {'stdout': signature, 'stderr': '', 'exitCode': 0})
    with pytest.raises(collector.CollectionError, match='unverified-rpm-signature'):
        collector.verify_signature(tmp_path / 'rpm', tmp_path / 'key', collector.ALMA_FINGERPRINT, tmp_path)


def test_unsupported_package_cannot_receive_evtest_execution_claim(tmp_path):
    artifact = {'digest': 'sha256:' + 'a' * 64, '_nativeIdentity': {'name': 'another-package'}}
    with pytest.raises(collector.CollectionError, match='unsupported-restricted-execution-profile'):
        collector.restricted_evtest(SimpleNamespace(), [artifact], {}, tmp_path)


def test_duplicate_json_keys_are_not_accepted_as_trust_inputs(tmp_path):
    source = tmp_path / 'input.json'
    source.write_text('{"schemaVersion":1,"schemaVersion":true}')
    with pytest.raises(collector.CollectionError):
        collector.load_json(source)


def collection_args(tmp_path, **overrides):
    args = {'output': str(tmp_path / 'output'), 'consumer_root': str(tmp_path),
            'consumer_base_reference': 'quay.io/almalinuxorg/almalinux@sha256:' + 'a' * 64,
            'source_revision': 'b' * 40, 'attempt_identity': 'gha:123:1',
            'baseline': 'x86-64-v2', 'scope': 'candidate', 'artifact': ['signed.rpm'],
            'unsigned_artifact': None, 'compiler_observation': str(tmp_path / 'compiler.json'),
            'candidate_key': None, 'candidate_fingerprint': None,
            'repository': ['https://repo.almalinux.org/almalinux/10/BaseOS/x86_64_v2/os/']}
    args.update(overrides)
    return SimpleNamespace(**args)


def fake_verifier():
    return SimpleNamespace(BaselineError=ValueError,
                           verify=lambda document: {'status': 'blocked', 'readiness': False,
                                                    'blockers': [{'code': 'no-artifacts'}]})


def test_signed_collection_requires_actual_pre_sign_artifact(tmp_path, monkeypatch):
    (tmp_path / 'usr/lib/sysimage/rpm').mkdir(parents=True)
    compiler = tmp_path / 'compiler.json'
    compiler.write_text('{"schemaVersion":1,"cpuBaseline":"x86-64-v2","flags":{"CFLAGS":"-march=x86-64-v2"}}')
    monkeypatch.setattr(collector, 'checked', lambda argv: {'stdout': 'glibc\t0\t1\t1\tx86_64_v2\n', 'stderr': '', 'exitCode': 0})
    observation = collector.collect(collection_args(tmp_path), fake_verifier())
    assert observation['status'] == 'blocked'
    assert observation['readiness'] is False
    assert 'missing-pre-sign-artifact-binding' in observation['blockers']
    assert (tmp_path / 'output/collection.json').is_file()


def test_arm_profile_blocks_without_fabricating_execution(tmp_path):
    observation = collector.collect(collection_args(tmp_path, baseline='armv8-a'), fake_verifier())
    assert observation['status'] == 'blocked'
    assert 'unsupported-native-arm-execution-profile' in observation['blockers']
    assert not (tmp_path / 'output/restricted-execution.json').exists()


def test_float_consumer_base_is_not_a_proof_input(tmp_path):
    with pytest.raises(collector.CollectionError, match='consumer-base-not-immutable'):
        collector.collect(collection_args(tmp_path, consumer_base_reference='almalinux:10'), fake_verifier())


def test_native_queries_pin_alma_database_instead_of_host_macro(tmp_path):
    (tmp_path / 'usr/lib/sysimage/rpm').mkdir(parents=True)
    command = collector.native_rpm_command(tmp_path, ['-qa', '--qf', collector.QUERY])
    assert command[:5] == ['rpm', '--root', str(tmp_path), '--dbpath', '/usr/lib/sysimage/rpm']


def test_missing_native_database_blocks_without_host_fallback(tmp_path):
    with pytest.raises(collector.CollectionError, match='native-rpm-database'):
        collector.native_rpm_command(tmp_path, ['-qa'])


def test_native_database_cannot_follow_symlink_outside_consumer(tmp_path):
    root = tmp_path / 'root'
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'usr/lib/sysimage').mkdir(parents=True)
    (root / 'usr/lib/sysimage/rpm').symlink_to(outside)
    with pytest.raises(collector.CollectionError, match='native-rpm-database'):
        collector.native_rpm_command(root, ['-qa'])


def test_vendor_input_suffix_cannot_match_publishable_rpm_glob(tmp_path):
    path = collector.vendor_input_path(tmp_path, 1)
    path.write_bytes(b'signed vendor input')
    assert path.name == 'native-1.rpm.input'
    assert list(tmp_path.rglob('*.rpm')) == []


def test_ci_real_restricted_cpu_has_positive_v2_and_negative_avx2_control(tmp_path):
    """Real execution controls run in CI; fixtures cannot establish CPU limits."""
    assert shutil.which('gcc'), 'CI must install gcc/static libc development files'
    assert Path('/usr/bin/qemu-x86_64-static').is_file(), 'CI must install qemu-user-static'
    (tmp_path / 'cpuid.c').write_text(collector.CPUID_SOURCE)
    (tmp_path / 'avx2.S').write_text(collector.AVX2_SOURCE)
    collector.checked(['gcc', '-static', '-march=x86-64', '-o', str(tmp_path / 'cpuid'), str(tmp_path / 'cpuid.c')])
    collector.checked(['gcc', '-nostdlib', '-static', '-o', str(tmp_path / 'avx2'), str(tmp_path / 'avx2.S')])
    command = ['/usr/bin/qemu-x86_64-static', '-cpu', 'Nehalem-v1']
    result = collector.checked(command + [str(tmp_path / 'cpuid')])
    assert all(value in result['stdout'] for value in ('v1=1', 'v2=1', 'avx=0', 'avx2=0', 'lahf=1', 'longmode=1'))
    assert collector.run(command + [str(tmp_path / 'avx2')])['exitCode'] == -4
