"""Manual attempts retain evidence without automatic continuation or promotion."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('single_planner', ROOT / 'scripts/plan-package-factory.py')
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)
SHA = 'a' * 40
CELL = {'id': 'gnome51-el10-x86_64', 'engine': 'build-chain', 'tiers': ''}


def run(monkeypatch, capsys, *args):
    monkeypatch.setattr(planner, 'all_cells', lambda root: [dict(CELL)])
    monkeypatch.setattr(planner, 'consumer_factory_plan', lambda *a: None)
    monkeypatch.setattr(planner.subprocess, 'run', lambda *a, **k: type('R', (), {'stdout': SHA})())
    monkeypatch.setattr(sys, 'argv', ['planner', *args])
    code = planner.main()
    output = capsys.readouterr()
    return code, json.loads(output.out) if code == 0 else output.err


def test_default_retains_three_shards(monkeypatch, capsys):
    code, result = run(monkeypatch, capsys, '--cell', CELL['id'])
    assert code == 0
    assert [len(json.loads(m)['include']) for m in result['matrices']] == [1, 1, 1]


def test_single_attempt_has_one_full_cell(monkeypatch, capsys):
    code, result = run(monkeypatch, capsys, '--cell', CELL['id'], '--single-attempt',
                       '--expected-source-revision', SHA)
    assert code == 0
    rows = [json.loads(m)['include'] for m in result['matrices']]
    assert [len(r) for r in rows] == [1, 0, 0]
    assert rows[0][0]['single_attempt'] is True
    assert rows[0][0]['tiers'] == ''
    assert result['batch_count'] == 1


@pytest.mark.parametrize('extra', [[], ['--expected-source-revision', 'b' * 40],
    ['--expected-source-revision', SHA, '--selector', 'engine=build-chain'],
    ['--expected-source-revision', SHA, '--batch-index', '1'],
    ['--expected-source-revision', SHA, '--canary-common'],
    ['--expected-source-revision', SHA, '--selection-digest', 'x']])
def test_invalid_scope_fails(monkeypatch, capsys, extra):
    assert run(monkeypatch, capsys, '--cell', CELL['id'], '--single-attempt', *extra)[0] == 2


@pytest.mark.parametrize('value', ['0', '1', '-1', 'unlimited', 'false'])
def test_single_attempt_does_not_accept_numeric_budget(monkeypatch, capsys, value):
    with pytest.raises(SystemExit) as exc:
        run(monkeypatch, capsys, '--single-attempt', value)
    assert exc.value.code == 2


def workflow():
    return yaml.safe_load((ROOT / '.github/workflows/package-factory-cell.yml').read_text())


def test_workflow_admission_and_promotion_boundaries():
    parent = (ROOT / '.github/workflows/package-factory.yml').read_text()
    assert '[ "$EVENT_NAME" = workflow_dispatch ]' in parent
    assert '[ -n "$REQUESTED_CELL" ]' in parent
    assert '[ -n "$REQUESTED_REVISION" ]' in parent
    assert '[ "$REQUESTED_REVISION" = "$GITHUB_SHA" ]' in parent
    steps = workflow()['jobs']['build']['steps']
    assert workflow()['jobs']['build']['timeout-minutes'] == 360
    for step in steps:
        if step.get('name', '').startswith(('Promote validated', 'Attest SLSA', 'Attest SPDX')):
            assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" in step['if']
    text = json.dumps(steps)
    assert 'oras-stage-artifacts.py' not in text
    assert 'publish-rpm-wave.sh' not in text
    assert 'cosign sign' not in text
    for name in ['Record a new ActionResult', 'Save validated output']:
        assert "chain_deferred != 'true'" in next(s['if'] for s in steps if s.get('name') == name)
    assert steps[-1]['name'] == 'Require complete output from a single attempt'
    assert any(s.get('with', {}).get('name', '').endswith('-partial') for s in steps[:-1])


@pytest.mark.parametrize(('deferred', 'outcome', 'hit', 'expected'), [
    ('true', 'success', 'false', 1), ('', 'failure', 'false', 1),
    ('', 'success', 'false', 0), ('', 'skipped', 'true', 0), ('', 'skipped', 'false', 1),
    ('', 'failure', 'true', 1), ('', '', '', 1), ('false', 'success', 'false', 1),
    ('true', 'skipped', 'true', 1), ('', 'success', 'true', 1)])
def test_terminal_gate_never_calls_partial_complete(deferred, outcome, hit, expected):
    import os
    script = workflow()['jobs']['build']['steps'][-1]['run']
    result = subprocess.run(['bash', '-c', script], env=dict(os.environ,
        CHAIN_DEFERRED=deferred, BUILD_OUTCOME=outcome, CACHE_HIT=hit, CACHE_VERDICT="success"), capture_output=True)
    assert result.returncode == expected


@pytest.mark.parametrize("verdict", ["", "failure", "skipped", "cancelled"])
def test_cache_hit_needs_successful_verification(verdict):
    import os
    script = workflow()['jobs']['build']['steps'][-1]['run']
    result = subprocess.run(['bash', '-c', script], env=dict(os.environ,
        CHAIN_DEFERRED='', BUILD_OUTCOME='skipped', CACHE_HIT='true',
        CACHE_VERDICT=verdict), capture_output=True)
    assert result.returncode == 1


def test_single_attempt_requires_exact_cell(monkeypatch, capsys):
    assert run(monkeypatch, capsys, '--single-attempt', '--expected-source-revision', SHA)[0] == 2


@pytest.mark.parametrize('patch', [{'engine': 'tideforge'}, {'tiers': 'base-tools'}, {'canary': True}])
def test_single_attempt_rejects_non_full_cell(monkeypatch, capsys, patch):
    monkeypatch.setitem(CELL, next(iter(patch)), next(iter(patch.values())))
    assert run(monkeypatch, capsys, '--cell', CELL['id'], '--single-attempt',
               '--expected-source-revision', SHA)[0] == 2


def test_mode_keeps_cell_identity_and_build_inputs(monkeypatch, capsys):
    _, normal = run(monkeypatch, capsys, '--cell', CELL['id'])
    _, single = run(monkeypatch, capsys, '--cell', CELL['id'], '--single-attempt',
                    '--expected-source-revision', SHA)
    a = json.loads(normal['matrices'][0])['include'][0]
    b = json.loads(single['matrices'][0])['include'][0]
    assert b.pop('single_attempt') is True
    assert a == b
    # Selection records the mode; unchanged build inputs still govern the action cache.
    assert normal['selection_digest'] != single['selection_digest']


@pytest.mark.parametrize('overrides', [
    {'EVENT_NAME': 'push'}, {'EVENT_NAME': 'pull_request'}, {'EVENT_NAME': 'schedule'},
    {'REQUESTED_CELL': ''}, {'REQUESTED_REVISION': ''}, {'REQUESTED_REVISION': 'b' * 40},
    {'REQUESTED_SELECTOR': 'family=gnome51'}, {'BATCH_INDEX': '1'},
    {'SELECTION_DIGEST': 'x'}, {'PARENT_RUN_ID': '1'}, {'PARENT_RUN_ATTEMPT': '1'},
    {'SINGLE_ATTEMPT': '0'}, {'SINGLE_ATTEMPT': 'unlimited'},
])
def test_manual_workflow_rejects_invalid_scope(overrides):
    import os
    parent = yaml.safe_load((ROOT / '.github/workflows/package-factory.yml').read_text())
    script = next(s['run'] for s in parent['jobs']['plan']['steps'] if s.get('id') == 'plan')
    script = script[script.index('if [ -n "$REQUESTED_REVISION" ]; then'):script.index('args+=(--batch-index')]
    env = dict(os.environ, SINGLE_ATTEMPT='true', EVENT_NAME='workflow_dispatch',
               REQUESTED_CELL=CELL['id'], REQUESTED_REVISION=SHA, GITHUB_SHA=SHA,
               REQUESTED_SELECTOR='', BATCH_INDEX='0', SELECTION_DIGEST='',
               PARENT_RUN_ID='', PARENT_RUN_ATTEMPT='')
    env.update(overrides)
    assert subprocess.run(['bash', '-c', 'set -euo pipefail; args=(); ' + script],
                          env=env, capture_output=True).returncode != 0
