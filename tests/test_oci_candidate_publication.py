"""The migration rehearsal must not become an implicit production cutover."""
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('candidate', ROOT / 'scripts/validate-oci-candidate.py')
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)


def run():
    return dict(repository={'full_name': 'tuna-os/tunaos-packages'},
                head_repository={'full_name': 'tuna-os/tunaos-packages'},
                head_sha='b' * 40, path='.github/workflows/publish-build-chain-rpms.yml',
                head_branch='main', event='workflow_dispatch', status='completed',
                conclusion='success')


def test_complete_and_partial_waves_are_accepted():
    for conclusion in ('success', 'failure'):
        value = run()
        value['conclusion'] = conclusion
        candidate.validate('gnome50-el10-x86_64', 'sha256:' + 'a' * 64, value,
                           'tuna-os/tunaos-packages')


@pytest.mark.parametrize('field,value', [
    ('repository', {'full_name': 'other/fork'}),
    ('head_repository', {'full_name': 'other/fork'}),
    ('head_sha', 'main'), ('head_sha', ''),
    ('path', '.github/workflows/untrusted.yml'),
    ('head_branch', 'feature'), ('event', 'pull_request'),
    ('status', 'in_progress'), ('conclusion', 'cancelled'),
    ('conclusion', None),
])
def test_untrusted_or_unfinished_wave_is_rejected(field, value):
    source = run()
    source[field] = value
    with pytest.raises(ValueError):
        candidate.validate('gnome50-el10-x86_64', 'sha256:' + 'a' * 64, source,
                           'tuna-os/tunaos-packages')


@pytest.mark.parametrize('digest', ['', 'latest', 'sha256:abc', 'sha256:' + 'G' * 64,
                                    'ghcr.io/other/image@sha256:' + 'a' * 64])
def test_no_empty_mutable_or_foreign_seed(digest):
    with pytest.raises(ValueError):
        candidate.validate('gnome50-el10-x86_64', digest, run(), 'tuna-os/tunaos-packages')


@pytest.mark.parametrize('cell', ['', '../escape', 'a,b', 'a; echo nope', 'a' * 97])
def test_exactly_one_safe_cell(cell):
    with pytest.raises(ValueError):
        candidate.validate(cell, 'sha256:' + 'a' * 64, run(), 'tuna-os/tunaos-packages')


def workflow():
    return yaml.safe_load((ROOT / '.github/workflows/publish-oci-candidate.yml').read_text())


def test_rehearsal_is_manual_main_only_and_uses_the_live_lock():
    doc = workflow()
    assert list(doc.get('on', doc.get(True))) == ['workflow_dispatch']
    assert doc['concurrency'] == {'group': 'publish-rpms', 'cancel-in-progress': False}
    assert doc['jobs']['candidate']['if'] == "github.ref == 'refs/heads/main'"


def test_no_r2_credentials_commands_or_implicit_promotion():
    text = (ROOT / '.github/workflows/publish-oci-candidate.yml').read_text()
    assert 'secrets.R2' not in text
    assert 'rclone' not in text
    assert 'candidate-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}' in text
    assert 'podman push' in text
    assert '"${image}:${CELL}"' not in text.split('podman push', 1)[1]
    assert '--evict-foreign' not in text
    assert '--allow-empty' not in text


def test_seed_verification_precedes_mutation_and_uses_exact_identity():
    steps = workflow()['jobs']['candidate']['steps']
    seed = next(s['run'] for s in steps if s.get('name', '').startswith('Verify and materialize'))
    assert 'test "${current##*@}" = "$SEED_DIGEST"' in seed
    assert 'publish-build-chain-rpms.yml@refs/heads/main' in seed
    assert seed.index('cosign verify') < seed.index('podman cp')
    assert 'gpg --batch --verify repo/repodata/repomd.xml.asc repo/repodata/repomd.xml' in seed
    assert 'test "$count" -gt 0' in seed
    assert '"$rpm_arch" = "$arch"' in seed
    merge = next(s['run'] for s in steps if s.get('name', '').startswith('Sign and merge'))
    assert 'scripts/publish-rpm-wave.sh --staged staged --repo repo --subdir build-chain' in merge
