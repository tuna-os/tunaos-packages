"""Real adapter command boundaries; cryptographic/native execution occurs in CI."""
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('alma_native_chain_adapter', ROOT / 'scripts/run-alma-recipe-chain.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


@pytest.fixture
def document():
    return {'target': 'alma10', 'architecture': 'x86_64',
        'image': 'example.org/native@sha256:' + 'a' * 64,
        'sourceRevision': 'b' * 40, 'queueDigest': 'sha256:' + 'c' * 64,
        'actionKey': 'sha256:' + 'd' * 64, 'baseDigest': 'sha256:' + 'a' * 64,
        'platform': 'linux/amd64/v2', 'cpuBaseline': 'x86-64-v2'}


def test_container_exact_base_arch_and_readonly_candidate(document, tmp_path):
    item = adapter.Adapter(document, ROOT, tmp_path)
    command = item.container(tmp_path / 'work', tmp_path / 'repo')
    assert command[:5] == ['podman', 'run', '--rm', '--platform', 'linux/amd64/v2']
    assert str((tmp_path / 'repo').resolve()) + ':/candidate-repo:ro' in command
    assert 'BUILD_IMAGE=' + document['image'] in command
    assert 'TUNAOS_CANDIDATE_REPO=/candidate-repo' in command
    assert str(ROOT / 'scripts') + ':/scripts:ro' in command
    assert 'TZ=UTC' in command and 'LANG=C.UTF-8' in command and 'LC_ALL=C.UTF-8' in command
    assert any(value.startswith('SOURCE_DATE_EPOCH=') for value in command)
    document['architecture'] = 'aarch64'
    document['platform'] = 'linux/arm64'
    assert adapter.Adapter(document, ROOT, tmp_path, 'docker').container(tmp_path, tmp_path)[4] == 'linux/arm64'


def test_container_unknown_engine_rejected(document, tmp_path):
    with pytest.raises(ValueError, match='unsupported'):
        adapter.Adapter(document, ROOT, tmp_path, 'sh')


def test_verifier_does_not_guess_artifacts_by_package_prefix(document, tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir(); (repo / 'app-old.rpm').write_bytes(b'unrelated')
    item = adapter.Adapter(document, ROOT, tmp_path)
    with pytest.raises(ValueError, match='missing'):
        item.verify({'name': 'app', 'requirements': {'build': [], 'runtime': []}}, repo)


def test_native_verifier_preserves_floors_exact_outputs_and_observation(document, tmp_path, monkeypatch):
    repo = tmp_path / 'repo'; repo.mkdir()
    for name in ('app-1.rpm', 'app-libs-1.rpm'):
        (repo / name).write_bytes(b'signed artifact')
    recipe = tmp_path / 'packages/app/package.yaml'
    recipe.parent.mkdir(parents=True)
    recipe.write_text('verify:\n  smoke: test -f /usr/bin/app\n')
    item = adapter.Adapter(document, tmp_path, tmp_path)
    item.outputs['app'] = ['app-1.rpm', 'app-libs-1.rpm']
    calls = []
    def fake_run(command, *, timeout):
        calls.append((command, timeout))
        proof = tmp_path / 'verification/app'
        for name in ('installed.tsv', 'transaction.txt', 'repositories.txt', 'smoke.log'):
            (proof / name).write_text('actual CI command output fixture')
    monkeypatch.setattr(adapter, 'run', fake_run)
    digests = item.verify({'name': 'app', 'recipe': 'packages/app/package.yaml', 'recipeDigest': adapter.file_digest(recipe),
        'requirements': {'build': ['rust >= 1.93'], 'runtime': ['library >= 2']}}, repo)
    proof = tmp_path / 'verification/app'
    assert (proof / 'requirements.txt').read_text() == 'library >= 2\nrust >= 1.93\n'
    assert (proof / 'packages.txt').read_text() == '/candidate-repo/app-1.rpm\n/candidate-repo/app-libs-1.rpm\n'
    assert (proof / 'smoke.sh').read_text() == 'set -eEuo pipefail\ntest -f /usr/bin/app\n'
    assert 'bash /proof/smoke.sh' in adapter.VERIFY
    assert calls[0][1] == 1800
    assert len(digests) == 1 and digests[0].startswith('sha256:')
    assert '"readiness": false' in (proof / 'observation.json').read_text()


def test_native_install_policy_never_disables_signatures():
    assert 'gpgcheck=1' in adapter.VERIFY and 'repo_gpgcheck=1' in adapter.VERIFY
    assert '--nogpgcheck' not in adapter.VERIFY
    assert 'dnf check' in adapter.VERIFY
    assert 'rpm --import /candidate-repo/candidate-public.gpg' in adapter.VERIFY


def test_prepare_is_actual_renderer_and_checksum_fetch_before_build(document, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, 'run', lambda command, **kwargs: calls.append(command))
    item = adapter.Adapter(document, ROOT, tmp_path)
    prepared = item.prepare({'name': 'app', 'recipe': 'packages/app/package.yaml'}, tmp_path / 'app', tmp_path / 'repo')
    assert prepared == tmp_path / 'app/rpmbuild'
    assert 'render' in calls[0]
    assert calls[1][1].endswith('fetch-tideforge-sources.py')
    assert all(command[0] != 'podman' for command in calls)
    item.restored_outputs({'name': 'app'}, [tmp_path / 'repo/app-1.rpm'])
    assert item.outputs['app'] == ['app-1.rpm']


def test_runner_candidate_optional_and_fixed_local_signed_scope():
    source = (ROOT / 'scripts/run-alma-rpm.sh').read_text()
    assert 'if [[ -n ${TUNAOS_CANDIDATE_REPO:-} ]]' in source
    assert '$TUNAOS_CANDIDATE_REPO == /candidate-repo' in source
    assert 'repo_gpgcheck=1' in source
    assert 'private candidate key forbidden' in source
    assert 'unsafe candidate repository entry' in source
    assert 'PUBLISHED_INDEX' in source
    assert source.index('native Alma architecture mismatch') < source.index('dnf -y')


def test_local_authored_rpms_require_dnf_signature_check():
    assert '--setopt=localpkg_gpgcheck=1 install "${packages[@]}"' in adapter.VERIFY
    assert 'localpkg_gpgcheck=1' in adapter.VERIFY


def test_single_deadline_starts_before_preparation(document, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter.signal, 'setitimer', lambda *args: calls.append(('deadline', args)))
    monkeypatch.setattr(adapter, 'run', lambda command, **kwargs: calls.append(('command', command)))
    item = adapter.Adapter(document, ROOT, tmp_path, package_seconds=4800)
    item.prepare({'name': 'app', 'recipe': 'packages/app/package.yaml'}, tmp_path / 'app', tmp_path / 'repo')
    assert calls[0] == ('deadline', (adapter.signal.ITIMER_REAL, 4800))
    assert calls[1][0] == 'command'


def test_timeout_removes_daemon_container(monkeypatch):
    calls = []
    def external(command, **kwargs):
        calls.append(command)
        if command[1] == 'run':
            raise adapter.subprocess.TimeoutExpired(command, 10)
    monkeypatch.setattr(adapter.subprocess, 'run', external)
    with pytest.raises(adapter.subprocess.TimeoutExpired):
        adapter.run(['docker', 'run', '--rm', 'native-image', 'bash'], timeout=10)
    assert calls[0][2] == '--name'
    assert calls[1] == ['docker', 'rm', '--force', calls[0][3]]


def test_digest_is_streamed(tmp_path):
    path = tmp_path / 'payload.rpm'
    path.write_bytes(b'abc')
    assert adapter.file_digest(path) == 'sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'


def test_package_deadline_accounts_for_wrapper_prephase(document, tmp_path, monkeypatch):
    timers = []
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: 20500)
    monkeypatch.setattr(adapter.signal, 'setitimer', lambda *args: timers.append(args))
    monkeypatch.setattr(adapter, 'run', lambda *args, **kwargs: None)
    item = adapter.Adapter(document, ROOT, tmp_path, package_seconds=4800, chain_started=100)
    item.prepare({'name': 'app', 'recipe': 'packages/app/package.yaml'}, tmp_path / 'app', tmp_path / 'repo')
    assert timers == [(adapter.signal.ITIMER_REAL, 600)]


def test_native_runner_calls_actual_vendor_guard_before_rpmbuild():
    source = (ROOT / 'scripts/run-alma-rpm.sh').read_text()
    assert 'python3 /factory/scripts/alma-rpmbuild-guard.py "$ARCHITECTURE" -ba' in source
    assert "--define '_topdir /work/rpmbuild'" in source
    wrapper = (ROOT / 'scripts/verify-alma-recipe-chain-ci.sh').read_text()
    assert wrapper.index('CHAIN_STARTED_MONOTONIC=') < wrapper.index('export CHAIN_STARTED_MONOTONIC\n') < wrapper.index('alma-candidate-resume.py prepare')


def test_smoke_cannot_be_substituted_by_changed_recipe(document, tmp_path):
    recipe = tmp_path / 'packages/app/package.yaml'
    recipe.parent.mkdir(parents=True)
    recipe.write_text('verify:\n  smoke: true\n')
    repo = tmp_path / 'repo'; repo.mkdir()
    item = adapter.Adapter(document, tmp_path, tmp_path)
    item.outputs['app'] = ['app-1.rpm']
    with pytest.raises(ValueError, match='authored digest'):
        item.verify({'name': 'app', 'recipe': 'packages/app/package.yaml',
            'recipeDigest': 'sha256:' + 'a' * 64, 'requirements': {'build': [], 'runtime': []}}, repo)


def test_container_cannot_flatten_alma_v2_variant(document, tmp_path):
    document['platform'] = 'linux/amd64'
    with pytest.raises(ValueError, match='platform identity'):
        adapter.Adapter(document, ROOT, tmp_path).container(tmp_path, tmp_path)
