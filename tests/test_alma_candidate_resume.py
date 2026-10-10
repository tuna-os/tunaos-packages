"""Resume bank and prepared-input checks; live attestation/signatures need CI."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/alma-candidate-resume.py'
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('alma_resume', SCRIPT)
resume = importlib.util.module_from_spec(spec); spec.loader.exec_module(resume)


@pytest.fixture
def candidate(tmp_path):
    meta = tmp_path / 'meta'; meta.mkdir()
    repo = tmp_path / 'repo'; repo.mkdir()
    build = tmp_path / 'prepared'; build.mkdir()
    (build / 'SPECS').mkdir(); (build / 'SOURCES').mkdir()
    (build / 'SPECS/native.spec').write_bytes(b'Version: 1\n')
    (build / 'SOURCES/source.tar').write_bytes(b'exact source bytes')
    identity = {'repository': 'tuna-os/tunaos-packages', 'sourceRevision': 'a' * 40,
        'workflow': '.github/workflows/package-factory.yml',
        'signerWorkflow': '.github/workflows/package-factory-cell.yml', 'sourceRef': 'refs/heads/main',
        'runId': 123, 'runAttempt': 1, 'cell': 'gnome50-alma10-x86_64', 'actionKey': 'sha256:' + 'b' * 64,
        'target': 'alma10', 'platform': {'os': 'linux', 'architecture': 'amd64', 'variant': 'v2'},
        'cpuBaseline': 'x86-64-v2', 'baseDigest': 'sha256:' + 'c' * 64}
    (meta / 'identity.json').write_text(json.dumps(identity))
    (meta / 'candidate-public.gpg').write_bytes(b'public key')
    (meta / 'candidate-identity.json').write_text(json.dumps({'fingerprint': 'A' * 40}))
    (repo / 'native.rpm').write_bytes(b'signed package')
    (repo / 'repodata').mkdir()
    (repo / 'repodata/repomd.xml').write_bytes(b'metadata')
    (repo / 'repodata/repomd.xml.asc').write_bytes(b'signature')
    return meta, repo, build, identity


def test_completion_skip_requires_exact_prepared_sources_and_outputs(candidate):
    meta, repo, build, _ = candidate
    assert resume.skip(meta, repo, 'native', build) is False
    resume.record(meta, repo, 'native', build, ['native.rpm'])
    assert resume.skip(meta, repo, 'native', build) is True
    (build / 'SOURCES/source.tar').write_bytes(b'changed remote content')
    assert resume.skip(meta, repo, 'native', build) is False


@pytest.mark.parametrize('change', ['spec', 'rpm', 'target', 'base', 'source', 'key', 'missing'])
def test_completion_mismatch_forces_rebuild(candidate, change):
    meta, repo, build, identity = candidate
    resume.record(meta, repo, 'native', build, ['native.rpm'])
    if change == 'spec': (build / 'SPECS/native.spec').write_bytes(b'Version: 2\n')
    elif change == 'rpm': (repo / 'native.rpm').write_bytes(b'tampered')
    elif change == 'missing': (repo / 'native.rpm').unlink()
    else:
        field = {'target': 'target', 'base': 'baseDigest', 'source': 'sourceRevision', 'key': 'actionKey'}[change]
        identity[field] = 'changed'
        (meta / 'identity.json').write_text(json.dumps(identity))
    assert resume.skip(meta, repo, 'native', build) is False


def test_prepared_input_identity_is_stable_across_attempts(candidate):
    meta, repo, build, identity = candidate
    resume.record(meta, repo, 'native', build, ['native.rpm'])
    identity['runId'] = 456; identity['runAttempt'] = 2
    (meta / 'identity.json').write_text(json.dumps(identity))
    assert resume.skip(meta, repo, 'native', build) is True


def test_snapshot_contains_complete_outputs_and_signed_metadata(candidate, tmp_path):
    meta, repo, build, identity = candidate
    resume.record(meta, repo, 'native', build, ['native.rpm'])
    (repo / 'buildroots').mkdir()
    (repo / 'buildroots/native.candidate-public.gpg').write_bytes(b'historical public key')
    (repo / 'buildroots/native.candidate-observed.txt').write_bytes(b'actual compiler observation')
    destination = tmp_path / 'snapshot'
    resume.create(meta, repo, destination, False)
    document = json.loads((destination / 'snapshot.json').read_text())
    assert document['productionReady'] is False
    assert document['chainComplete'] is False
    assert document['identity'] == identity
    assert [entry['path'] for entry in document['inventory']] == [
        'buildroots/native.candidate-observed.txt', 'candidate-public.gpg', 'native.rpm',
        'repodata/repomd.xml', 'repodata/repomd.xml.asc']
    assert document['completedPackages'][0]['outputs'] == [{'path': 'native.rpm',
        'digest': 'sha256:' + hashlib.sha256(b'signed package').hexdigest()}]


def test_unrecorded_rpm_cannot_enter_snapshot(candidate, tmp_path):
    meta, repo, _, _ = candidate
    with pytest.raises(ValueError, match='completed packages'):
        resume.create(meta, repo, tmp_path / 'snapshot', False)
    assert not (tmp_path / 'snapshot').exists()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'payload/../escape', 'other.json'])
def test_archive_unsafe_names_are_rejected_before_extraction(tmp_path, name):
    archive = tmp_path / 'snapshot.zip'
    with zipfile.ZipFile(archive, 'w') as zipped: zipped.writestr(name, b'bytes')
    destination = tmp_path / 'destination'; destination.mkdir()
    with pytest.raises(ValueError): resume.extract(archive, destination)
    assert list(destination.iterdir()) == []


def test_archive_link_entries_are_rejected_before_extraction(tmp_path):
    archive = tmp_path / 'snapshot.zip'
    info = zipfile.ZipInfo('payload/link'); info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, 'w') as zipped: zipped.writestr(info, b'../escape')
    destination = tmp_path / 'destination'; destination.mkdir()
    with pytest.raises(ValueError): resume.extract(archive, destination)
    assert list(destination.iterdir()) == []


def test_admission_verification_failure_cannot_seed_completions(candidate, tmp_path, monkeypatch):
    meta, repo, _, _ = candidate
    def rejected(command, **kwargs): raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(resume.subprocess, 'run', rejected)
    with pytest.raises(subprocess.CalledProcessError): resume.admit(meta, repo, tmp_path / 'state', tmp_path / 'envelope')
    assert not (meta / 'completed-packages.json').exists()


def test_workflow_banks_attested_native_partials_without_production_promotion():
    workflow = yaml.safe_load((ROOT / '.github/workflows/package-factory-cell.yml').read_text())
    steps = workflow['jobs']['build']['steps']
    attestation = next(step for step in steps if step.get('id') == 'native_snapshot_attestation')
    assert attestation['uses'] == 'actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6'
    assert attestation['with']['subject-path'].endswith('/native-snapshot/snapshot.json')
    upload = next(step for step in steps if step.get('name') == 'Bank authenticated native Alma progress')
    assert '${{ github.run_id }}-${{ github.run_attempt }}' in upload['with']['name']
    assert 'overwrite' not in upload['with']
    prepare = next(step for step in steps if step.get('name') == 'Prepare authenticated Alma candidate resume')
    assert "github.event_name == 'workflow_dispatch'" in prepare['if']
    assert 'pull_request' not in prepare['if']
    promotion = next(step for step in steps if step.get('name', '').startswith('Promote validated ActionResult'))
    assert "github.ref == 'refs/heads/main'" in promotion['if']
    legacy = next(step for step in steps if step.get('name') == "Resume from a previous attempt's partial output")
    assert "!startsWith(matrix.mock_config, 'alma10-')" in legacy['if']


@pytest.mark.parametrize('url', [
    'http://productionresultssa19.blob.core.windows.net/actions-results/archive',
    'https://example.com/archive?sig=secret',
    'https://productionresultssa19.blob.core.windows.net.evil.example/archive',
    'https://user:password@productionresultssa19.blob.core.windows.net/archive',
    'https://productionresultssa19.blob.core.windows.net:444/archive',
    'https://productionresultssa19.blob.core.windows.net/archive#fragment'])
def test_signed_artifact_redirect_policy_rejects_unapproved_urls(url):
    with pytest.raises(ValueError): resume.validate_artifact_url(url)


def test_public_artifact_host_is_allowed_without_forwarding_credentials():
    resume.validate_artifact_url('https://productionresultssa19.blob.core.windows.net/actions-results/archive.zip?sig=secret')


def test_download_refuses_second_redirect_and_keeps_credentials_on_api_only(candidate, tmp_path, monkeypatch):
    import urllib.error
    meta, _, _, identity = candidate
    calls = []
    signed_url = 'https://productionresultssa19.blob.core.windows.net/actions-results/archive.zip?sig=SECRET'
    class Opener:
        def open(self, request, **kwargs):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, 'redirect', {'Location': signed_url}, None)
            raise urllib.error.HTTPError(signed_url, 302, 'second redirect', {'Location': 'https://evil.example/'}, None)
    monkeypatch.setenv('GH_TOKEN', 'API-TOKEN')
    monkeypatch.setattr(resume.urllib.request, 'build_opener', lambda handler: Opener())
    with pytest.raises(urllib.error.HTTPError):
        resume.download(identity['repository'], 42, tmp_path / 'archive.zip')
    assert calls[0].get_header('Authorization') == 'Bearer API-TOKEN'
    assert calls[0].get_header('X-github-api-version') == '2026-03-10'
    assert calls[1] == signed_url


def test_restore_diagnostic_does_not_print_signed_url(candidate, tmp_path, monkeypatch, capsys):
    import urllib.error
    meta, _, _, identity = candidate
    artifact = {'id': 42, 'name': identity['cell'] + '-alma-candidate-123-1-ci', 'expired': False,
                'workflow_run': {'head_sha': identity['sourceRevision'], 'id': 123}}
    requests = []
    def github_boundary(command, **kwargs):
        requests.append(command[-1])
        if '/workflows/' in command[-1]:
            document = {'workflow_runs': [{'id': 123, 'head_sha': identity['sourceRevision'], 'path': identity['workflow']}]}
        else:
            document = {'artifacts': [artifact]}
        kwargs['stdout'].write(json.dumps(document).encode())
        return subprocess.CompletedProcess(command, 0)
    class Opener:
        def open(self, request, **kwargs):
            raise urllib.error.HTTPError('https://productionresultssa19.blob.core.windows.net/file?sig=SECRET',
                                          403, 'signed-url SECRET', {}, None)
    monkeypatch.setattr(resume.subprocess, 'run', github_boundary)
    monkeypatch.setattr(resume.urllib.request, 'build_opener', lambda handler: Opener())
    monkeypatch.setenv('GH_TOKEN', 'API-TOKEN')
    resume.restore(meta, tmp_path / 'destination')
    output = capsys.readouterr()
    assert 'SECRET' not in output.err
    assert 'blob.core.windows.net' not in output.err
    assert 'HTTPError' in output.err
    assert requests == [
        'repos/tuna-os/tunaos-packages/actions/workflows/package-factory.yml/runs?head_sha=' + 'a' * 40 + '&exclude_pull_requests=true&per_page=30&page=1',
        'repos/tuna-os/tunaos-packages/actions/runs/123/artifacts?per_page=30&page=1']
    assert not (tmp_path / 'destination').exists()


def test_live_ci_requires_two_distinct_manual_dispatches_at_same_sha():
    workflow = yaml.safe_load((ROOT / '.github/workflows/alma-candidate-resume-ci.yml').read_text())
    # PyYAML's YAML 1.1 loader interprets the workflow's `on` as True.
    trigger = workflow.get('on', workflow.get(True))
    assert trigger['workflow_dispatch']['inputs']['phase']['options'] == ['produce', 'resume']
    steps = workflow['jobs']['native-resume']['steps']
    assert any(step.get('run') == 'bash scripts/verify-alma-candidate-resume-ci.sh' for step in steps)
    assert workflow['permissions']['contents'] == 'read'
    assert workflow['permissions']['attestations'] == 'write'
    script = (ROOT / 'scripts/verify-alma-candidate-resume-ci.sh').read_text()
    assert "receipt['producer']['sourceRevision'] == os.environ['GITHUB_SHA']" in script
    assert "receipt['producer']['runId'] != int(os.environ['GITHUB_RUN_ID'])" in script
    assert 'repo_gpgcheck=1' in script
    assert '--nogpgcheck' not in script


def test_discovery_has_one_shared_ten_request_budget(candidate, tmp_path, monkeypatch, capsys):
    meta, _, _, identity = candidate
    calls = []
    def github_boundary(command, **kwargs):
        calls.append(command[-1])
        if '/workflows/' in command[-1]:
            document = {'workflow_runs': [{'id': number, 'head_sha': identity['sourceRevision'],
                        'path': identity['workflow']} for number in range(30)]}
        else:
            document = {'artifacts': [{'id': number, 'name': 'unrelated', 'expired': False}
                                     for number in range(30)]}
        kwargs['stdout'].write(json.dumps(document).encode())
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(resume.subprocess, 'run', github_boundary)
    resume.restore(meta, tmp_path / 'destination')
    assert len(calls) == 10
    assert len([call for call in calls if '/workflows/' in call]) == 1
    assert len([call for call in calls if '/artifacts?' in call]) == 9
    assert 'No authenticated matching Alma snapshot' in capsys.readouterr().out
    assert not (tmp_path / 'destination').exists()
