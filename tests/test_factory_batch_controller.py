"""Exact batch coverage at the GitHub CLI boundary.

Falsification: a successful child with wrong source, attempt, selection or
origin cannot close the parent factory's coverage gap. No receipt is readiness.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/factory-batches.py'
REPO = 'tuna-os/tunaos-packages'
PREFIX = f'repos/{REPO}/actions/'
SHA = 'a' * 40


def packed(document, filename):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(filename, json.dumps(document))
    return base64.b64encode(stream.getvalue()).decode()


def plan():
    inventory = [{'id': 'cell-a'}, {'id': 'cell-b'}]
    digest = 'sha256:' + hashlib.sha256(json.dumps({'sourceRevision': SHA, 'cells': inventory},
                                                 sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'source_revision': SHA, 'selection_inventory': inventory, 'selection_count': 2,
            'selection_digest': digest, 'batch_count': 2, 'batch_index': 0,
            'planned_batches': [{'batch_index': 0, 'cells': ['cell-a']},
                                {'batch_index': 1, 'cells': ['cell-b']}]}


@pytest.fixture
def controller(tmp_path):
    binq = tmp_path / 'bin'; binq.mkdir()
    executable = binq / 'gh'
    executable.write_text('''#!/usr/bin/env python3
import base64,json,os,pathlib,sys
args=sys.argv[1:];endpoint=args[args.index('-H')+2]
with open(os.environ['API_LOG'],'a') as log:
    payload=json.loads(pathlib.Path(args[args.index('--input')+1]).read_text()) if '--input' in args else None
    log.write(json.dumps({'endpoint':endpoint,'payload':payload,'version':args[args.index('-H')+1]})+'\\n')
responses=json.loads(pathlib.Path(os.environ['API_RESPONSES']).read_text())
if endpoint not in responses: sys.exit(7)
value=responses[endpoint]
if isinstance(value,dict) and value.get('_error'): sys.exit(8)
if isinstance(value,dict) and '_zip' in value: sys.stdout.buffer.write(base64.b64decode(value['_zip']))
else: print(json.dumps(value))
''')
    executable.chmod(0o755)
    selected = plan(); digest = selected['selection_digest']
    receipt = {'schemaVersion': 1, 'sourceRevision': SHA, 'selectionDigest': digest,
               'batchIndex': 1, 'batchCount': 2, 'parentRunId': 100, 'parentAttempt': 2,
               'runId': 200, 'runAttempt': 3, 'status': 'success', 'readiness': False}
    dispatch = {'sourceRevision': SHA, 'selectionDigest': digest, 'parentRunId': 100,
                'parentAttempt': 2, 'readiness': False,
                'batches': [{'batchIndex': 1, 'runId': 200, 'cells': ['cell-b']}]}
    run = {'id': 200, 'head_sha': SHA, 'event': 'workflow_dispatch', 'status': 'completed',
           'conclusion': 'success', 'path': '.github/workflows/package-factory.yml',
           'repository': {'full_name': REPO}, 'head_repository': {'full_name': REPO}, 'run_attempt': 3}
    responses = {PREFIX+'runs/200': run,
                 PREFIX+'workflows/package-factory.yml/dispatches': {'workflow_run_id': 200}}
    def add_artifacts(run_id, artifacts):
        rows=[]
        for number,(name,document,filename) in enumerate(artifacts,run_id+1):
            rows.append({'id':number,'name':name,'expired':False,'size_in_bytes':1000,
                         'workflow_run': {'id':run_id,'head_sha':SHA,'head_branch':'main'}})
            responses[PREFIX+f'artifacts/{number}/zip']={'_zip':packed(document,filename)}
        responses[PREFIX+f'runs/{run_id}/artifacts?per_page=100&page=1']={'artifacts':rows}
    add_artifacts(100,[('factory-plan-0-attempt2',selected,'factory-plan.json'),
                       ('factory-batch-dispatch-attempt2',dispatch,'factory-batch-dispatch.json')])
    add_artifacts(200,[('factory-batch-result-1-attempt3',receipt,'factory-batch-result.json')])
    env={**os.environ,'PATH':str(binq)+':'+os.environ['PATH'],'GITHUB_REPOSITORY':REPO,
         'GITHUB_SHA':SHA,'GITHUB_RUN_ID':'100','GITHUB_RUN_ATTEMPT':'2','GITHUB_EVENT_NAME':'workflow_dispatch',
         'GITHUB_REF_NAME':'main','SELECTION_DIGEST':digest,'BATCH_COUNT':'2','BATCH_INDEX':'1',
         'PARENT_RUN_ID':'100','PARENT_RUN_ATTEMPT':'2','BATCH_STATUS':'success',
         'API_RESPONSES':str(tmp_path/'responses.json'),'API_LOG':str(tmp_path/'api.log')}
    return {'directory':tmp_path,'responses':responses,'env':env,'plan':selected,'receipt':receipt,'dispatch':dispatch}


def invoke(controller, operation, **changes):
    c=controller
    Path(c['env']['API_RESPONSES']).write_text(json.dumps(c['responses']))
    command=[sys.executable,str(SCRIPT),operation]
    if operation=='dispatch':
        file=c['directory']/'plan.json';file.write_text(json.dumps(c['plan']))
        command+=['--plan',str(file)]
    return subprocess.run(command,cwd=c['directory'],env={**c['env'],**changes},capture_output=True,text=True)


def replace_receipt(controller, **changes):
    receipt={**controller['receipt'],**changes}
    controller['responses'][PREFIX+'artifacts/201/zip']={'_zip':packed(receipt,'factory-batch-result.json')}


def test_exact_completed_child_closes_only_build_coverage(controller):
    result=invoke(controller,'verify')
    assert result.returncode==0,result.stderr
    assert not (controller['directory']/'factory-batch-result.json').exists()
    assert controller['receipt']['readiness'] is False


@pytest.mark.parametrize('field,value', [('head_sha','b'*40),('event','pull_request'),('status','in_progress'),
    ('conclusion','failure'),('conclusion','cancelled'),('path','.github/workflows/other.yml'),('id',201),
    ('repository',{'full_name':'attacker/fork'}),('head_repository',{'full_name':'attacker/fork'})])
def test_failed_pending_or_forked_child_never_satisfies_coverage(controller,field,value):
    controller['responses'][PREFIX+'runs/200'][field]=value
    assert invoke(controller,'verify').returncode!=0


@pytest.mark.parametrize('field,value', [('runAttempt',2),('runId',201),('sourceRevision','b'*40),
    ('selectionDigest','sha256:'+'0'*64),('parentAttempt',1),('parentRunId',99),('batchIndex',0),
    ('batchCount',3),('status','failure'),('readiness',True),('schemaVersion',2)])
def test_receipt_must_match_exact_parent_child_source_and_attempt(controller,field,value):
    replace_receipt(controller,**{field:value})
    assert invoke(controller,'verify').returncode!=0


def test_boolean_schema_version_is_not_integer_version_one(controller):
    replace_receipt(controller,schemaVersion=True)
    assert invoke(controller,'verify').returncode!=0


@pytest.mark.parametrize('condition',['missing','expired','duplicate','wrong_attempt','fork_metadata'])
def test_missing_expired_ambiguous_or_untrusted_artifact_blocks(controller,condition):
    rows=controller['responses'][PREFIX+'runs/200/artifacts?per_page=100&page=1']['artifacts']
    if condition=='missing': rows.clear()
    elif condition=='expired': rows[0]['expired']=True
    elif condition=='duplicate': rows.append(copy.deepcopy(rows[0]))
    elif condition=='wrong_attempt': rows[0]['name']='factory-batch-result-1-attempt2'
    else: rows[0]['workflow_run']={'id':999,'head_sha':'b'*40,'head_branch':'evil'}
    assert invoke(controller,'verify').returncode!=0


@pytest.mark.parametrize('mutation',['hash','missing_cell','duplicate_cell','source','index'])
def test_dispatch_rejects_noncanonical_or_incomplete_partition_before_api(controller,mutation):
    document=controller['plan']
    if mutation=='hash': document['selection_digest']='sha256:'+'0'*64
    elif mutation=='missing_cell': document['planned_batches'][1]['cells']=[]
    elif mutation=='duplicate_cell': document['planned_batches'][1]['cells']=['cell-a']
    elif mutation=='source': document['source_revision']='b'*40
    else: document['planned_batches'][1]['batch_index']=2
    result=invoke(controller,'dispatch')
    assert result.returncode!=0
    assert not Path(controller['env']['API_LOG']).exists()


def test_dispatch_records_response_run_id_and_exact_request_binding(controller):
    result=invoke(controller,'dispatch',REQUESTED_CELL='alma10-x86_64',REQUESTED_SELECTOR='gnome')
    assert result.returncode==0,result.stderr
    record=json.loads((controller['directory']/'factory-batch-dispatch.json').read_text())
    assert record['batches']==[{'batchIndex':1,'runId':200,'cells':['cell-b']}]
    call=json.loads(Path(controller['env']['API_LOG']).read_text())
    assert call['version']=='X-GitHub-Api-Version: 2026-03-10'
    assert call['payload']=={'ref':'main','inputs':{'batch_index':'1','source_revision':SHA,
           'selection_digest':controller['plan']['selection_digest'],'parent_run_id':'100',
           'parent_run_attempt':'2','cell':'alma10-x86_64','selector':'gnome'}}
    assert record['readiness'] is False


@pytest.mark.parametrize('response',[{}, {'workflow_run_id':True},{'workflow_run_id':0},{'workflow_run_id':100}])
def test_invalid_or_parent_dispatch_id_never_records_a_child(controller,response):
    controller['responses'][PREFIX+'workflows/package-factory.yml/dispatches']=response
    assert invoke(controller,'dispatch').returncode!=0
    record=json.loads((controller['directory']/'factory-batch-dispatch.json').read_text())
    assert record['batches']==[]


def test_api_failure_remains_blocked_instead_of_empty_success(controller):
    controller['responses'][PREFIX+'runs/200']={'_error':True}
    assert invoke(controller,'verify').returncode!=0


def test_result_emits_exact_versioned_attempt_and_no_readiness(controller):
    result=invoke(controller,'result',GITHUB_RUN_ID='200',GITHUB_RUN_ATTEMPT='3')
    assert result.returncode==0,result.stderr
    assert json.loads((controller['directory']/'factory-batch-result.json').read_text())==controller['receipt']


@pytest.mark.parametrize('changes',[{'BATCH_STATUS':'healthy'},{'BATCH_INDEX':'2'},{'SELECTION_DIGEST':'floating-main'}])
def test_invalid_result_state_selection_or_batch_bounds_is_rejected(controller,changes):
    assert invoke(controller,'result',**changes).returncode!=0
    assert not (controller['directory']/'factory-batch-result.json').exists()


def test_workflow_gate_collects_exact_receipts_and_reports_without_green_waivers():
    workflow=yaml.safe_load((ROOT/'.github/workflows/package-factory.yml').read_text())
    gate=workflow['jobs']['gate']
    verification=next(step for step in gate['steps'] if 'factory-batches.py verify' in step.get('run',''))
    assert verification['env']['SOURCE_REVISION']=='${{ needs.plan.outputs.source_revision }}'
    assert verification['env']['SELECTION_DIGEST']=='${{ needs.plan.outputs.selection_digest }}'
    assert verification.get('continue-on-error',False) is False
    receipt=next(step for step in gate['steps'] if 'factory-batches.py result' in step.get('run',''))
    assert 'always()' in receipt['if']
    assert receipt['env']['PARENT_RUN_ATTEMPT']=='${{ inputs.parent_run_attempt || github.run_attempt }}'
    assert receipt['env']['BATCH_STATUS']=='${{ job.status }}'


def test_workflow_path_with_exact_dispatched_branch_is_accepted(controller):
    controller['responses'][PREFIX+'runs/200']['path']='.github/workflows/package-factory.yml@main'
    result=invoke(controller,'verify')
    assert result.returncode==0,result.stderr


def test_other_workflow_branch_suffix_is_untrusted(controller):
    controller['responses'][PREFIX+'runs/200']['path']='.github/workflows/package-factory.yml@evil'
    assert invoke(controller,'verify').returncode!=0


def three_batches(controller):
    selected=controller['plan']
    selected['selection_inventory'].append({'id':'cell-c'})
    selected['selection_count']=3
    selected['batch_count']=3
    selected['planned_batches'].append({'batch_index':2,'cells':['cell-c']})
    selected['selection_digest']='sha256:'+hashlib.sha256(json.dumps(
        {'sourceRevision':SHA,'cells':[{'id':'cell-a'},{'id':'cell-b'},{'id':'cell-c'}]},
        sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return selected['selection_digest']


def test_dispatch_rejects_repeated_child_ids_before_claiming_full_partition(controller):
    three_batches(controller)
    result=invoke(controller,'dispatch')
    assert result.returncode!=0
    record=json.loads((controller['directory']/'factory-batch-dispatch.json').read_text())
    assert record['batches']==[{'batchIndex':1,'runId':200,'cells':['cell-b']}]


def test_verification_rejects_one_run_reused_as_two_required_batches(controller):
    digest=three_batches(controller)
    dispatch={**controller['dispatch'],'selectionDigest':digest,'batches':[
        {'batchIndex':1,'runId':200,'cells':['cell-b']},
        {'batchIndex':2,'runId':200,'cells':['cell-c']} ]}
    controller['responses'][PREFIX+'artifacts/101/zip']={'_zip':packed(controller['plan'],'factory-plan.json')}
    controller['responses'][PREFIX+'artifacts/102/zip']={'_zip':packed(dispatch,'factory-batch-dispatch.json')}
    replace_receipt(controller,selectionDigest=digest,batchCount=3)
    assert invoke(controller,'verify',BATCH_COUNT='3',SELECTION_DIGEST=digest).returncode!=0
