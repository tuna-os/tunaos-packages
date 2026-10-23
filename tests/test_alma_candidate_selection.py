"""Candidate selection executes production Python, never native build/publication."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT/'scripts/verify-alma-recipe-chain-ci.sh'
WORKFLOW = ROOT/'.github/workflows/alma-recipe-chain-ci.yml'
SHA = 'a'*40


@pytest.fixture
def selection_checkout(tmp_path):
    shutil.copytree(ROOT/'scripts'/'factory', tmp_path/'scripts'/'factory')
    shutil.copy2(ROOT/'scripts'/'alma-recipe-chain.py', tmp_path/'scripts'/'alma-recipe-chain.py')
    for name, targets in [('cosmic-icon-theme', ['alma10']), ('tuna-desktop', ['alma10', 'alma10-kitten']),
                          ('foreign', ['arch'])]:
        path=tmp_path/'packages'/name/'package.yaml'
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump({'name':name, 'targets':targets}))
    return tmp_path


def select(checkout, scope, target='alma10', arch='x86_64'):
    # Run the actual wrapper's first Python phase; no builder, GPG or Docker.
    script = WRAPPER.read_text().split("<<'PY_SELECTION'\n",1)[1].split('\nPY_SELECTION',1)[0]
    destination=checkout/'selection.json'
    destination.unlink(missing_ok=True)
    result=subprocess.run([sys.executable,'-c',script,target,arch,scope,SHA,str(destination)],
                          cwd=checkout,capture_output=True,text=True)
    return result, destination


@pytest.mark.parametrize(('scope','roots'), [('canary',['cosmic-icon-theme']),
                                           ('full',['cosmic-icon-theme','tuna-desktop']),
                                           ('tuna',['tuna-desktop'])])
def test_production_wrapper_selection_keeps_bounded_roots(selection_checkout, scope, roots):
    result,path=select(selection_checkout,scope)
    assert result.returncode == 0,result.stderr
    value=json.loads(path.read_text())
    assert value['requestedRoots'] == roots
    assert value['actionKey'].startswith('sha256:') and len(value['actionKey']) == 71


@pytest.mark.parametrize(('target','arch','scope'), [('alma10','x86_64','unknown'),
                                                   ('el10','x86_64','tuna'),
                                                   ('alma10','s390x','tuna')])
def test_wrapper_rejects_bad_identity_without_selection_receipt(selection_checkout,target,arch,scope):
    result,path=select(selection_checkout,scope,target,arch)
    assert result.returncode != 0
    assert not path.exists()


def test_full_roots_change_production_action_key(selection_checkout):
    result,path=select(selection_checkout,'full')
    assert result.returncode == 0
    before=json.loads(path.read_text())
    recipe=selection_checkout/'packages/extra/package.yaml'
    recipe.parent.mkdir()
    recipe.write_text("name: extra\ntargets: [alma10]\n")
    result,path=select(selection_checkout,'full')
    assert result.returncode == 0
    after=json.loads(path.read_text())
    assert after['requestedRoots'] != before['requestedRoots']
    assert after['actionKey'] != before['actionKey']


def test_duplicate_full_root_is_refused(selection_checkout):
    duplicate=selection_checkout/'packages/duplicate/package.yaml'
    duplicate.parent.mkdir()
    duplicate.write_text("name: tuna-desktop\ntargets: [alma10]\n")
    result,path=select(selection_checkout,'full')
    assert result.returncode != 0
    assert not path.exists()


def test_workflow_dispatch_remains_native_candidate_only():
    workflow=yaml.safe_load(WORKFLOW.read_text())
    trigger=workflow.get('on',workflow.get(True))
    scope=trigger['workflow_dispatch']['inputs']['scope']
    assert scope['default'] == 'canary'
    assert scope['options'] == ['canary','full','tuna']
    native=workflow['jobs']['native']
    assert native['if'] == "github.event_name == 'workflow_dispatch'"
    assert native['strategy']['matrix'] == {'target':['alma10','alma10-kitten'],
                                          'architecture':['x86_64','aarch64']}
    assert 'ubuntu-24.04-arm' in native['runs-on']
    assert workflow['permissions'] == {'contents':'read','actions':'read',
                                      'id-token':'write','attestations':'write'}
    text=WORKFLOW.read_text()
    assert 'secrets.' not in text and 'rclone' not in text
    wrapper=WRAPPER.read_text()
    assert wrapper.index('alma-recipe-chain.py --target') < wrapper.index('candidate-rpm-repository.py init')
    assert "'signer_workflow':'.github/workflows/alma-recipe-chain-ci.yml'" in wrapper
    assert '/selection.json' in text
