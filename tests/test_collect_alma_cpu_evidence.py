"""CI CPU collection must prove bytes, signatures and restricted execution.

Falsification: substitute an unknown mirror, package identity, signing key,
installed ELF, or an unrestricted CPU control; collection must fail closed.
These tests are authored for CI. Local build/test execution is not authorized.
"""
import gzip
import importlib.util
import io
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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


@pytest.mark.parametrize('arch', ['x86_64_v2', 'aarch64'])
def test_kitten_proof_uses_the_native_release_repository(arch):
    """Falsification: the /10 alias failed collection after signed installation."""
    url = f'https://kitten.repo.almalinux.org/10-kitten/BaseOS/{arch}/os/'
    assert collector.repository_url(url) == url
    with pytest.raises(collector.CollectionError):
        collector.repository_url(url.replace('/10-kitten/', '/10/'))


def test_root_collected_public_evidence_is_retained_on_failure():
    """Falsification: CI upload could not read the collector's root-owned GPG DB."""
    script = (Path(__file__).parents[1] / 'scripts/verify-alma-evtest-ci.sh').read_text()
    cleanup = script.split('cleanup() {', 1)[1].split('trap cleanup EXIT', 1)[0]
    assert 'local cleanup_status=$?' in cleanup
    assert 'sudo chown -R "$(id -u):$(id -g)" "$out/metadata/cpu-proof"' in cleanup
    assert 'rm -rf -- "$state_parent"' in cleanup
    assert 'exit "$cleanup_status"' in cleanup
    assert 'sudo chown -R "$(id -u):$(id -g)" "$state_parent"' not in cleanup


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


REPOSITORY = 'https://repo.almalinux.org/almalinux/10/BaseOS/x86_64_v2/os/'
RPM_URL = REPOSITORY + 'Packages/cache-proof.rpm'


@pytest.fixture
def network(monkeypatch):
    """Replace HTTPS transport, not fetching, hashing, metadata parsing or proof."""
    bodies, requests = {}, []

    class Opener:
        def open(self, url, timeout):
            assert timeout == 60
            requests.append(url)
            response = io.BytesIO(bodies[url])
            response.headers = {'Last-Modified': 'Sat, 31 Oct 2026 00:00:00 GMT'}
            return response

    monkeypatch.setattr(collector.urllib.request, 'build_opener', lambda *args: Opener())
    return bodies, requests


def cache_path(cache, data):
    return cache / 'sha256' / collector.digest(data)[7:]


def seed_blob(cache, expected, contents=None):
    path = cache_path(cache, expected)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(expected if contents is None else contents)
    return path


def publish_metadata(bodies, rpm, identity):
    primary = gzip.compress((
        '<metadata xmlns="http://linux.duke.edu/metadata/common">'
        '<package type="rpm"><name>{name}</name><arch>{architecture}</arch>'
        '<version epoch="{epoch}" ver="{version}" rel="{release}"/>'
        '<checksum type="sha256">{checksum}</checksum>'
        '<location href="Packages/cache-proof.rpm"/></package></metadata>'
    ).format(**identity, checksum=collector.digest(rpm)[7:]).encode())
    bodies[REPOSITORY + 'repodata/repomd.xml'] = (
        '<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="primary">'
        '<checksum type="sha256">' + collector.digest(primary)[7:] + '</checksum>'
        '<location href="repodata/primary.xml.gz"/></data></repomd>'
    ).encode()
    bodies[REPOSITORY + 'repodata/primary.xml.gz'] = primary
    bodies[RPM_URL] = rpm
    return primary


@pytest.mark.parametrize('bad_entry', ['tampered', 'partial', 'oversize', 'symlink', 'directory'])
def test_untrusted_cache_entry_refetches_bounded_authenticated_bytes(tmp_path, network, bad_entry):
    bodies, requests = network
    expected = b'upstream RPM bytes'
    bodies[RPM_URL] = expected
    cache = tmp_path / 'cache'
    path = cache_path(cache, expected)
    path.parent.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.write_bytes(expected)
    if bad_entry == 'symlink':
        path.symlink_to(outside)
    elif bad_entry == 'directory':
        path.mkdir()
    else:
        path.write_bytes({'tampered': b'forged', 'partial': expected[:4],
                          'oversize': b'x' * 129}[bad_entry])
    destination = tmp_path / 'rpm.input'
    result = collector.fetch_cached(RPM_URL, destination, 128, collector.digest(expected), cache)
    assert result['fromCache'] is False
    assert destination.read_bytes() == expected
    assert requests == [RPM_URL]
    assert outside.read_bytes() == expected
    if bad_entry != 'directory':
        assert path.read_bytes() == expected
        assert not path.is_symlink()


def test_cache_directory_symlink_never_redirects_writes(tmp_path, network):
    bodies, requests = network
    bodies[RPM_URL] = b'input'
    outside = tmp_path / 'outside'
    outside.mkdir()
    cache = tmp_path / 'cache'
    cache.mkdir()
    (cache / 'sha256').symlink_to(outside, target_is_directory=True)
    collector.fetch_cached(RPM_URL, tmp_path / 'rpm', 128, collector.digest(b'input'), cache)
    assert requests == [RPM_URL]
    assert list(outside.iterdir()) == []


def test_fresh_repomd_changes_both_primary_and_rpm_cache_identity(tmp_path, network):
    bodies, requests = network
    identity = {'name': 'cache-proof', 'epoch': '0', 'version': '1',
                'release': '1', 'architecture': 'x86_64_v2'}
    cache = tmp_path / 'cache'
    for index, rpm in enumerate((b'old bytes', b'new upstream bytes')):
        primary = publish_metadata(bodies, rpm, identity)
        repository = collector.Repository(REPOSITORY, tmp_path / f'repo-{index}', cache)
        match = repository.find(identity)
        result = collector.fetch_cached(match['url'], tmp_path / f'rpm-{index}', 128,
                                        match['digest'], cache)
        assert result['digest'] == collector.digest(rpm)
        assert not result['fromCache']
        assert cache_path(cache, primary).read_bytes() == primary
    assert requests.count(REPOSITORY + 'repodata/repomd.xml') == 2
    assert requests.count(REPOSITORY + 'repodata/primary.xml.gz') == 2
    assert requests.count(RPM_URL) == 2


def test_network_digest_mismatch_is_failure_not_a_cache_fallback(tmp_path, network):
    bodies, requests = network
    bodies[RPM_URL] = b'network corruption'
    cache = tmp_path / 'cache'
    seed_blob(cache, b'expected', b'bad cache')
    with pytest.raises(collector.CollectionError, match='download-digest-mismatch'):
        collector.fetch_cached(RPM_URL, tmp_path / 'rpm', 128, collector.digest(b'expected'), cache)
    assert requests == [RPM_URL]
    assert cache_path(cache, b'expected').read_bytes() == b'bad cache'


def test_interrupted_atomic_write_is_not_a_cache_hit(tmp_path, monkeypatch, network):
    bodies, requests = network
    bodies[RPM_URL] = expected = b'complete input'
    cache = tmp_path / 'cache'
    replace = collector.os.replace

    def interrupted(*args, **kwargs):
        raise OSError('interrupted before atomic rename')

    monkeypatch.setattr(collector.os, 'replace', interrupted)
    first = collector.fetch_cached(RPM_URL, tmp_path / 'first', 128,
                                   collector.digest(expected), cache)
    assert not first['fromCache']
    assert not cache_path(cache, expected).exists()
    assert list((cache / 'sha256').iterdir()) == []
    # A killed writer can leave a temporary file; readers never consider it.
    (cache / 'sha256' / '.partial-killed-writer').write_bytes(expected[:3])
    monkeypatch.setattr(collector.os, 'replace', replace)
    second = collector.fetch_cached(RPM_URL, tmp_path / 'second', 128,
                                    collector.digest(expected), cache)
    third = collector.fetch_cached(RPM_URL, tmp_path / 'third', 128,
                                   collector.digest(expected), cache)
    assert not second['fromCache']
    assert third['fromCache']
    assert requests == [RPM_URL, RPM_URL]
    assert (tmp_path / 'third').read_bytes() == expected


def test_concurrent_same_digest_writers_publish_one_complete_blob(tmp_path, network):
    bodies, _ = network
    bodies[RPM_URL] = expected = b'concurrent input'
    cache = tmp_path / 'cache'

    def acquire(index):
        destination = tmp_path / f'rpm-{index}'
        collector.fetch_cached(RPM_URL, destination, 128, collector.digest(expected), cache)
        return destination.read_bytes()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(acquire, range(4))) == [expected] * 4
    assert cache_path(cache, expected).read_bytes() == expected
    assert list((cache / 'sha256').iterdir()) == [cache_path(cache, expected)]


def test_default_api_never_reuses_prior_download(tmp_path, network):
    bodies, requests = network
    bodies[RPM_URL] = expected = b'input'
    for index in range(2):
        result = collector.fetch_cached(RPM_URL, tmp_path / str(index), 128,
                                        collector.digest(expected))
        assert not result['fromCache']
    assert requests == [RPM_URL, RPM_URL]


@pytest.fixture(scope='module')
def signed_cache_packages(tmp_path_factory):
    """Real ELF RPMs and an ephemeral test signer, not production Alma authority."""
    directory = tmp_path_factory.mktemp('cache-rpms')
    spec = importlib.util.spec_from_file_location(
        'cache_cpu_verifier', Path(__file__).parents[1] / 'scripts/verify-cpu-baseline.py')
    cpu = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cpu)
    for tool in ('gcc', 'rpmbuild', 'rpmsign', 'rpm2cpio', 'gpg'):
        assert shutil.which(tool), f'CI must install {tool}'
    library = directory / 'libcache-proof.so'
    (directory / 'library.c').write_text('int proof(void) { return 0; }\n')
    (directory / 'program.c').write_text('extern int proof(void); void _start(void) { proof(); }\n')
    collector.checked(['gcc', '-shared', '-nostdlib', '-Wl,-soname,libcache-proof.so',
                       '-o', str(library), str(directory / 'library.c')])
    executable = directory / 'evtest'
    collector.checked(['gcc', '-nostdlib', '-no-pie', '-Wl,--no-as-needed',
                       '-o', str(executable), str(directory / 'program.c'), str(library)])
    rpms = {}
    for name, source, installed in (('evtest', executable, '/usr/bin/evtest'),
                                    ('cache-proof', library, '/usr/lib64/libcache-proof.so')):
        top = directory / 'buildroots' / name
        for subdirectory in ('BUILD', 'BUILDROOT', 'RPMS', 'SOURCES', 'SPECS', 'SRPMS'):
            (top / subdirectory).mkdir(parents=True)
        recipe = top / 'SPECS' / f'{name}.spec'
        recipe.write_text(
            f'Name: {name}\nVersion: 1\nRelease: 1\nSummary: CPU cache test\nLicense: MIT\n'
            'AutoReqProv: no\n%description\nCPU cache test\n%install\n'
            f'mkdir -p %{{buildroot}}{Path(installed).parent}\n'
            f'cp {source} %{{buildroot}}{installed}\n%files\n{installed}\n')
        collector.checked(['rpmbuild', '--define', '_topdir ' + str(top),
                           '--define', '_build_id_links none',
                           '--define', '__os_install_post %{nil}',
                           '--define', '_rpmfilename %{NAME}.rpm', '-bb', str(recipe)])
        rpms[name] = top / 'RPMS' / f'{name}.rpm'
    unsigned = directory / 'unsigned-evtest.rpm'
    unsigned.write_bytes(rpms['evtest'].read_bytes())
    unsigned_vendor = rpms['cache-proof'].read_bytes()
    home = directory / 'gnupg'
    home.mkdir(mode=0o700)
    collector.checked(['gpg', '--homedir', str(home), '--batch', '--pinentry-mode', 'loopback',
                       '--passphrase', '', '--quick-generate-key', 'Alma input cache test',
                       'rsa2048', 'sign', '1d'])
    key = directory / 'key.asc'
    key.write_text(collector.checked(
        ['gpg', '--homedir', str(home), '--armor', '--export'])['stdout'])
    fingerprint, _ = collector.key_fingerprint(key, directory / 'show-key')
    try:
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv('GNUPGHOME', str(home))
            for rpm in rpms.values():
                collector.checked(['rpmsign', '--define', '_gpg_name ' + fingerprint,
                                   '--define', '_gpg_path ' + str(home),
                                   '--define', '__gpg /usr/bin/gpg',
                                   '--addsign', str(rpm)])
        yield SimpleNamespace(cpu=cpu, rpms=rpms, unsigned=unsigned,
                              unsigned_vendor=unsigned_vendor, key=key, fingerprint=fingerprint)
    finally:
        collector.run(['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'])


@pytest.mark.parametrize('failure', [
    'unsigned-vendor', 'installed-byte-mismatch', 'restricted-profile'])
def test_collection_cache_hits_still_authenticate_signature_and_bind_bytes(
        tmp_path, monkeypatch, network, signed_cache_packages, failure):
    packages = signed_cache_packages
    bodies, requests = network
    cpu = packages.cpu
    root = tmp_path / 'consumer'
    (root / 'usr/lib/sysimage/rpm').mkdir(parents=True)
    identities = {name: cpu.rpm_identity(rpm) for name, rpm in packages.rpms.items()}
    for rpm in packages.rpms.values():
        for name, body in cpu.rpm_elfs(rpm):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
    if failure == 'installed-byte-mismatch':
        with (root / 'usr/lib64/libcache-proof.so').open('ab') as target:
            target.write(b'installed corruption')
    compiler = tmp_path / 'compiler.json'
    compiler.write_text('{"schemaVersion":1,"cpuBaseline":"x86-64-v2",'
                        '"flags":{"CFLAGS":"-march=x86-64-v2"}}')
    vendor = (packages.unsigned_vendor if failure == 'unsigned-vendor'
              else packages.rpms['cache-proof'].read_bytes())
    primary = publish_metadata(bodies, vendor, identities['cache-proof'])
    bodies[collector.ALMA_KEY] = packages.key.read_bytes()
    cache = tmp_path / 'cache'
    seed_blob(cache, primary)
    seed_blob(cache, vendor)
    monkeypatch.setattr(collector, 'ALMA_FINGERPRINT', packages.fingerprint)
    real_checked = collector.checked
    inventory = '\n'.join('\t'.join(identity[field] for field in collector.IDENTITY_FIELDS)
                          for identity in identities.values()) + '\n'

    def native_inventory(argv):
        # Only the installed RPM database is synthetic. Real RPM parsing,
        # GPG/RPM signature checks, payload extraction and ELF binding run.
        if argv[:2] == ['rpm', '--root']:
            if '-qa' in argv:
                stdout = inventory
            else:
                identity = identities['cache-proof' if '--whatprovides' in argv else 'evtest']
                stdout = '\t'.join(identity[field] for field in collector.IDENTITY_FIELDS) + '\n'
            return {'stdout': stdout, 'stderr': '', 'exitCode': 0}
        return real_checked(argv)

    monkeypatch.setattr(collector, 'checked', native_inventory)
    result = collector.collect(collection_args(
        tmp_path, consumer_root=str(root), artifact=[str(packages.rpms['evtest'])],
        unsigned_artifact=[str(packages.unsigned)], candidate_key=str(packages.key),
        candidate_fingerprint=packages.fingerprint, repository=[REPOSITORY], cache_dir=cache), cpu)
    # No network transport for either cached blob; trust roots are still fresh.
    assert requests == [collector.ALMA_KEY, REPOSITORY + 'repodata/repomd.xml']
    assert result['repositories'][0]['primary']['fromCache'] is True
    expected = {'unsigned-vendor': 'unverified-rpm-signature',
                'installed-byte-mismatch': 'installed-elf-byte-mismatch',
                'restricted-profile': 'ambiguous-restricted-loader-or-executable'}[failure]
    assert expected in result['blockers']
    assert result['readiness'] is False
    if failure != 'unsigned-vendor':
        signature = json.loads((tmp_path / 'output/native-signature-1.json').read_text())
        assert signature['fingerprint'] == packages.fingerprint
        assert signature['artifactDigest'] == collector.digest(vendor)
        assert 'Payload SHA256 digest: OK' in signature['verification']['stdout']
    if failure == 'restricted-profile':
        assert 'restricted-execution' in result['phaseSeconds']
        assert (tmp_path / 'output/native-elf-1.json').is_file()
