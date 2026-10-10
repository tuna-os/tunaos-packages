"""CPU labels cannot establish Alma v2 package compatibility.

Falsification: replace exact ELF or dependency evidence with a v3 requirement,
missing execution, or an unrelated digest; every substitution must block.
"""
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('cpu_baseline', Path(__file__).parents[1] / 'scripts/verify-cpu-baseline.py')
cpu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpu)
D = 'sha256:' + 'a' * 64


def fixture(tmp_path, level=2, baseline='x86-64-v2'):
    path = tmp_path / 'fixture.rpm'
    path.write_bytes(b'fixture bytes')
    identity = cpu.digest(path.read_bytes())
    document = {'schemaVersion': 1, 'baseline': baseline, 'artifacts': [
        {'path': str(path), 'digest': identity, 'dependencyDigests': [],
         'compiler': {'artifactDigest': identity, 'evidenceDigest': D,
                      'flags': ['-march=x86-64-v2' if baseline == 'x86-64-v2' else '-march=armv8-a']}}]}
    def inspect(data):
        return {'digest': cpu.digest(data), 'machine': cpu.BASELINES[baseline][0], 'isaNeeded': level}
    return document, inspect


def check(document, inspect):
    return cpu.verify(document, inspect=inspect, extract=lambda path: [('usr/bin/example', b'ELF')])


def test_compatible_v2_evidence_is_bound_but_not_authenticated_readiness(tmp_path):
    document, inspect = fixture(tmp_path)
    result = check(document, inspect)
    assert result['status'] == 'evidence-bound'
    assert result['readiness'] is False


@pytest.mark.parametrize('baseline,flag,blocked', [
    ('armv8-a', '-mbranch-protection=standard', False),
    ('armv8-a', '-mbranch-protection=standard+leaf', True),
    ('armv8-a', '-mbranch-protection=unknown', True),
    ('x86-64-v2', '-mbranch-protection=standard', True),
])
def test_measured_arm_hardening_has_an_exact_architecture_scoped_exception(tmp_path, baseline, flag, blocked):
    document, inspect = fixture(tmp_path, baseline=baseline)
    document['artifacts'][0]['compiler']['flags'].append(flag)
    codes = check(document, inspect)['artifacts'][0]['blockers']
    assert ('incompatible-compiler-baseline' in codes) is blocked


def test_v3_required_isa_blocks_v2_even_with_v2_flags(tmp_path):
    document, inspect = fixture(tmp_path, 3)
    assert 'incompatible-elf-isa' in check(document, inspect)['artifacts'][0]['blockers']


def test_tampered_artifact_digest_blocks(tmp_path):
    document, inspect = fixture(tmp_path)
    Path(document['artifacts'][0]['path']).write_bytes(b'tampered')
    assert 'artifact-digest-mismatch' in check(document, inspect)['artifacts'][0]['blockers']


def test_absent_isa_property_requires_exact_restricted_execution(tmp_path):
    document, inspect = fixture(tmp_path, None)
    assert 'missing-exact-restricted-execution' in check(document, inspect)['artifacts'][0]['blockers']
    artifact = document['artifacts'][0]
    artifact['execution'] = {'artifactDigest': artifact['digest'], 'baseline': 'x86-64-v2',
                             'runnerDigest': D, 'evidenceDigest': D, 'exitCode': 0,
                             'elfDigests': [cpu.digest(b'ELF')]}
    assert check(document, inspect)['status'] == 'evidence-bound'
    artifact['execution']['elfDigests'] = [D]
    assert check(document, inspect)['status'] == 'blocked'


def test_dependency_artifacts_must_be_present_and_compatible(tmp_path):
    document, inspect = fixture(tmp_path)
    document['artifacts'][0]['dependencyDigests'] = [D]
    assert 'missing-dependency-artifact' in check(document, inspect)['artifacts'][0]['blockers']
    dependency = tmp_path / 'library.rpm'
    dependency.write_bytes(b'library')
    identity = cpu.digest(dependency.read_bytes())
    document['artifacts'][0]['dependencyDigests'] = [identity]
    document['artifacts'].append({'path': str(dependency), 'digest': identity, 'dependencyDigests': [],
                                  'compiler': {'artifactDigest': identity, 'evidenceDigest': D, 'flags': ['-march=x86-64-v3']}})
    assert check(document, inspect)['status'] == 'blocked'


def test_arm_native_behavior_requires_actual_execution_when_static_absent(tmp_path):
    document, inspect = fixture(tmp_path, None, 'armv8-a')
    artifact = document['artifacts'][0]
    assert check(document, inspect)['status'] == 'blocked'
    artifact['execution'] = {'artifactDigest': artifact['digest'], 'baseline': 'armv8-a',
                             'runnerDigest': D, 'evidenceDigest': D, 'exitCode': 0,
                             'elfDigests': [cpu.digest(b'ELF')]}
    assert check(document, inspect)['status'] == 'evidence-bound'


@pytest.mark.parametrize('version', [True, 2, '1'])
def test_unsupported_schema_versions_block(tmp_path, version):
    document, inspect = fixture(tmp_path)
    document['schemaVersion'] = version
    with pytest.raises(cpu.BaselineError):
        check(document, inspect)


def test_archive_parser_never_extracts_untrusted_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(cpu, 'command_bytes', lambda command: b'not cpio')
    with pytest.raises(cpu.BaselineError):
        cpu.rpm_elfs(tmp_path / 'anything.rpm')


@pytest.mark.parametrize('flags', [['-march=broadwell'], ['-march=x86-64-v2', '-mcpu=generic'],
                                   ['-march=x86-64-v2', '-Ctarget-feature=+avx'],
                                   ['-march=x86-64-v2', 'GOAMD64=v3'],
                                   ['-march=x86-64-v2', '-mavx2']])
def test_unknown_or_higher_compiler_overrides_block(tmp_path, flags):
    document, inspect = fixture(tmp_path)
    document['artifacts'][0]['compiler']['flags'] = flags
    assert check(document, inspect)['status'] == 'blocked'


@pytest.mark.parametrize('field', ['dependencyDigests', 'elfDigests'])
def test_malformed_nested_digest_arrays_produce_blockers(tmp_path, field):
    document, inspect = fixture(tmp_path, None)
    artifact = document['artifacts'][0]
    if field == 'dependencyDigests':
        artifact[field] = [{}]
    else:
        artifact['execution'] = {'artifactDigest': artifact['digest'], 'baseline': 'x86-64-v2',
                                 'runnerDigest': D, 'evidenceDigest': D, 'exitCode': 0,
                                 'elfDigests': [{}]}
    assert check(document, inspect)['status'] == 'blocked'


def test_actual_gcc_readelf_linked_library_requires_exact_provider(tmp_path):
    import shutil
    import subprocess
    if not shutil.which('gcc') or not shutil.which('readelf'):
        pytest.skip('native gcc/readelf unavailable')
    source = tmp_path / 'library.c'
    source.write_text('int example(void) {return 1;}\n')
    library = tmp_path / 'libexample.so'
    subprocess.run(['gcc', '-shared', '-nostdlib', '-march=x86-64-v2', '-Wl,-soname,libexample.so',
                    str(source), '-o', str(library)], check=True)
    source.write_text('extern int example(void); int use(void) {return example();}\n')
    consumer = tmp_path / 'consumer.so'
    subprocess.run(['gcc', '-shared', '-nostdlib', '-march=x86-64-v2', str(source), '-L'+str(tmp_path),
                    '-lexample', '-o', str(consumer)], check=True)
    assert cpu.inspect_elf(consumer.read_bytes())['needed'] == ['libexample.so']
    assert cpu.inspect_elf(library.read_bytes())['soname'] == 'libexample.so'
    document = {'schemaVersion': 1, 'baseline': 'x86-64-v2', 'artifacts': []}
    for path in (consumer, library):
        identity = cpu.digest(path.read_bytes())
        document['artifacts'].append({'path': str(path), 'digest': identity, 'dependencyDigests': [],
            'compiler': {'artifactDigest': identity, 'flags': ['-march=x86-64-v2'], 'evidenceDigest': D},
            'execution': {'artifactDigest': identity, 'baseline': 'x86-64-v2', 'runnerDigest': D,
                          'evidenceDigest': D, 'exitCode': 0, 'elfDigests': [identity]}})
    extract = lambda path: [(path.name, path.read_bytes())]
    assert cpu.verify(document, extract=extract)['status'] == 'blocked'
    library_digest = document['artifacts'][1]['digest']
    document['artifacts'][0]['dependencyDigests'] = [library_digest]
    document['artifacts'][0]['libraryProviders'] = {'libexample.so': library_digest}
    assert cpu.verify(document, extract=extract)['status'] == 'evidence-bound'
    document['artifacts'][0]['libraryProviders'] = {'libexample.so': D}
    assert cpu.verify(document, extract=extract)['status'] == 'blocked'


def newc(name, payload=b'', nlink=1, magic=b'070701', inode=1, mode=0o100644, device=0):
    encoded = name.encode() + b'\0'
    fields = [inode, mode, 0, 0, nlink, 0, len(payload), device, 0, 0, 0, len(encoded), 0]
    data = magic + b''.join(f'{value:08x}'.encode() for value in fields) + encoded
    data += b'\0' * (-len(data) % 4)
    data += payload
    data += b'\0' * (-len(data) % 4)
    return data


def test_pure_data_payload_distinguished_from_unsupported_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(cpu, 'command_bytes', lambda command: newc('usr/share/data', b'data') + newc('TRAILER!!!'))
    assert cpu.rpm_elfs(tmp_path / 'fixture.rpm') == []


@pytest.mark.parametrize('archive', [newc('usr/bin/link', nlink=2) + newc('TRAILER!!!'),
                                     newc('usr/bin/crc', magic=b'070702') + newc('TRAILER!!!'),
                                     newc('usr/share/data', b'data')])
def test_crc_hardlinks_and_truncated_archives_fail_closed(tmp_path, monkeypatch, archive):
    monkeypatch.setattr(cpu, 'command_bytes', lambda command: archive)
    with pytest.raises(cpu.BaselineError):
        cpu.rpm_elfs(tmp_path / 'fixture.rpm')


def test_native_glibc_hardlink_aliases_are_inspected_without_extraction(tmp_path, monkeypatch):
    """Falsification: authentic Alma glibc stores one ELF across three newc aliases."""
    payload = b'\x7fELFbounded-fixture'
    names = ['usr/libexec/getconf/POSIX_V6_LP64_OFF64',
             'usr/libexec/getconf/POSIX_V7_LP64_OFF64',
             'usr/libexec/getconf/XBS5_LP64_OFF64']
    archive = b''.join(newc(name, payload if index == 2 else b'', nlink=3, inode=92)
                       for index, name in enumerate(names)) + newc('TRAILER!!!')
    monkeypatch.setattr(cpu, 'command_bytes', lambda command: archive)
    assert cpu.rpm_elfs(tmp_path / 'fixture.rpm') == [(name, payload) for name in names]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('members', [
    [newc('first', nlink=2), newc('second', nlink=3)],
    [newc('first', nlink=2), newc('second', nlink=2, mode=0o100755)],
    [newc('first', b'\x7fELFone', nlink=2), newc('second', b'\x7fELFtwo', nlink=2)],
    [newc('first', nlink=2), newc('second', nlink=2), newc('third', nlink=2)],
    [newc('first', nlink=2), newc('second', nlink=2, device=1)],
    [newc('first', nlink=2), newc('second', nlink=2), newc('third', nlink=1)],
])
def test_ambiguous_or_incomplete_hardlink_groups_remain_blocked(tmp_path, monkeypatch, members):
    """Falsification: aliases cannot hide conflicting data, metadata or link counts."""
    monkeypatch.setattr(cpu, 'command_bytes', lambda command: b''.join(members) + newc('TRAILER!!!'))
    with pytest.raises(cpu.BaselineError):
        cpu.rpm_elfs(tmp_path / 'fixture.rpm')


def vendor_fixture(tmp_path):
    document, inspect = fixture(tmp_path)
    artifact = document['artifacts'][0]
    artifact.pop('compiler')
    native = {'name': 'vendor-library', 'epoch': '0', 'version': '1', 'release': '1.alma', 'architecture': 'x86_64_v2'}
    artifact['vendorProof'] = {'artifactDigest': artifact['digest'], 'baseline': 'x86-64-v2',
                               'nativeIdentity': native.copy(), 'repositorySnapshotDigest': D,
                               'signingIdentity': 'AlmaLinux pinned signing fingerprint',
                               'signatureEvidenceDigest': D, 'baselineEvidenceDigest': D}
    def check_vendor():
        return cpu.verify(document, inspect=inspect, extract=lambda path: [('library', b'ELF')],
                          native_identity=lambda path: native)
    return document, native, check_vendor


def test_authenticated_vendor_input_does_not_require_invented_compiler_flags(tmp_path):
    document, native, check_vendor = vendor_fixture(tmp_path)
    result = check_vendor()
    assert result['status'] == 'evidence-bound'
    assert result['readiness'] is False


@pytest.mark.parametrize('field', ['artifactDigest', 'repositorySnapshotDigest', 'signatureEvidenceDigest',
                                   'baselineEvidenceDigest', 'signingIdentity', 'nativeIdentity', 'baseline'])
def test_vendor_missing_bound_evidence_blocks(tmp_path, field):
    document, native, check_vendor = vendor_fixture(tmp_path)
    del document['artifacts'][0]['vendorProof'][field]
    assert check_vendor()['status'] == 'blocked'


@pytest.mark.parametrize('field', ['repositorySnapshotDigest', 'signatureEvidenceDigest', 'baselineEvidenceDigest'])
def test_vendor_malformed_evidence_digest_blocks(tmp_path, field):
    document, native, check_vendor = vendor_fixture(tmp_path)
    document['artifacts'][0]['vendorProof'][field] = {'digest': D}
    assert check_vendor()['status'] == 'blocked'


def test_vendor_tampered_native_identity_or_artifact_binding_blocks(tmp_path):
    document, native, check_vendor = vendor_fixture(tmp_path)
    document['artifacts'][0]['vendorProof']['nativeIdentity']['version'] = 'unrelated'
    assert check_vendor()['status'] == 'blocked'
    document['artifacts'][0]['vendorProof']['nativeIdentity'] = native.copy()
    document['artifacts'][0]['vendorProof']['artifactDigest'] = D
    assert check_vendor()['status'] == 'blocked'


def test_vendor_proof_does_not_waive_execution_or_library_closure(tmp_path):
    document, native, _ = vendor_fixture(tmp_path)
    inspect = lambda data: {'digest': cpu.digest(data), 'machine': cpu.BASELINES['x86-64-v2'][0],
                            'isaNeeded': None, 'needed': ['libmissing.so'], 'soname': None}
    result = cpu.verify(document, inspect=inspect, extract=lambda path: [('library', b'ELF')],
                        native_identity=lambda path: native)
    blockers = result['artifacts'][0]['blockers']
    assert 'missing-exact-restricted-execution' in blockers
    assert 'unbound-or-ambiguous-linked-library' in blockers
