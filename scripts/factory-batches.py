#!/usr/bin/env python3
"""Dispatch exact factory batches and reject incomplete batch coverage."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import tempfile
import zipfile

REPOSITORY = 'tuna-os/tunaos-packages'
WORKFLOW = 'package-factory.yml'
from github_api import API_VERSION
LIMIT = 4 * 1024 * 1024


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def decode(data):
    if len(data) > LIMIT:
        raise ValueError('oversized batch evidence')
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def integer(value, minimum=1):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
        raise ValueError('invalid batch integer')
    value = int(value)
    if value < minimum:
        raise ValueError('batch integer outside bounds')
    return value


def context():
    if os.environ.get('GITHUB_REPOSITORY', '').lower() != REPOSITORY:
        raise ValueError('unexpected factory repository')
    source = os.environ.get('SOURCE_REVISION') or os.environ.get('GITHUB_SHA', '')
    if not re.fullmatch(r'[0-9a-f]{40}', source):
        raise ValueError('immutable factory source required')
    return source, integer(os.environ['GITHUB_RUN_ID']), integer(os.environ['GITHUB_RUN_ATTEMPT'])


def api(endpoint, body=None, raw=False):
    if not endpoint.startswith(f'repos/{REPOSITORY}/actions/'):
        raise ValueError('unapproved batch API endpoint')
    command = ['gh', 'api', '--hostname', 'github.com', '-H', f'X-GitHub-Api-Version: {API_VERSION}', endpoint]
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (LIMIT, LIMIT))
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        if body is None:
            result = subprocess.run(command, stdout=output, stderr=errors, timeout=60, preexec_fn=limits)
        else:
            with tempfile.NamedTemporaryFile(mode='w') as payload:
                json.dump(body, payload, allow_nan=False)
                payload.flush()
                result = subprocess.run(command + ['--method', 'POST', '--input', payload.name],
                                        stdout=output, stderr=errors, timeout=60, preexec_fn=limits)
        if result.returncode:
            raise ValueError('batch API failed; coverage remains blocked')
        output.seek(0)
        data = output.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('oversized batch API response')
    return data if raw else decode(data)


def validate_plan(plan, source):
    if not isinstance(plan, dict):
        raise ValueError('factory plan object required')
    inventory, batches = plan.get('selection_inventory'), plan.get('planned_batches')
    if (plan.get('source_revision') != source or not isinstance(inventory, list)
            or not isinstance(batches, list) or not batches or len(batches) > 64
            or plan.get('selection_count') != len(inventory) or plan.get('batch_count') != len(batches)):
        raise ValueError('incomplete factory batch plan')
    if (any(type(plan.get(field)) is not int for field in ('selection_count', 'batch_count', 'batch_index'))
            or not 0 <= plan['batch_index'] < len(batches)):
        raise ValueError('factory plan integer types required')
    canonical = json.dumps({'sourceRevision': source, 'cells': inventory},
                           sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    expected = 'sha256:' + hashlib.sha256(canonical).hexdigest()
    if plan.get('selection_digest') != expected:
        raise ValueError('batch selection digest mismatch')
    if any(not isinstance(cell, dict) or not isinstance(cell.get('id'), str) or not cell['id'] for cell in inventory):
        raise ValueError('factory cell identities required')
    ids = [cell['id'] for cell in inventory]
    assigned = []
    for index, batch in enumerate(batches):
        if (not isinstance(batch, dict) or type(batch.get('batch_index')) is not int
                or batch['batch_index'] != index or not isinstance(batch.get('cells'), list)
                or any(not isinstance(cell, str) or not cell for cell in batch['cells'])):
            raise ValueError('invalid batch assignment')
        assigned.extend(batch['cells'])
    if len(set(ids)) != len(ids) or sorted(ids) != sorted(assigned):
        raise ValueError('batch assignments lose or repeat selected cells')
    return expected, batches


def artifact(run, attempt, name):
    if name.startswith('factory-plan-'):
        expected_file = 'factory-plan.json'
    elif name.startswith('factory-batch-dispatch-'):
        expected_file = 'factory-batch-dispatch.json'
    elif name.startswith('factory-batch-result-'):
        expected_file = 'factory-batch-result.json'
    else:
        raise ValueError('unapproved batch artifact name')
    documents = []
    for page in range(1, 11):
        response = api(f'repos/{REPOSITORY}/actions/runs/{run}/artifacts?per_page=100&page={page}')
        rows = response.get('artifacts')
        if not isinstance(rows, list):
            raise ValueError('invalid batch artifact inventory')
        documents.extend(row for row in rows if row.get('name') == name)
        if len(rows) < 100:
            break
    else:
        raise ValueError('truncated batch artifact inventory')
    if len(documents) != 1 or documents[0].get('expired') is not False:
        raise ValueError('missing or ambiguous batch artifact')
    record = documents[0]
    producer = record.get('workflow_run')
    if not isinstance(producer, dict) or type(producer.get('id')) is not int or producer['id'] != run:
        raise ValueError('batch artifact producer mismatch')
    source = os.environ.get('SOURCE_REVISION') or os.environ.get('GITHUB_SHA', '')
    if producer.get('head_sha') != source:
        raise ValueError('batch artifact source mismatch')
    if integer(record.get('size_in_bytes')) > LIMIT:
        raise ValueError('oversized batch artifact')
    data = api(f'repos/{REPOSITORY}/actions/artifacts/{integer(record["id"])}/zip', raw=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if names != [expected_file]:
            raise ValueError('invalid batch artifact archive')
        info = archive.getinfo(names[0])
        if info.file_size > LIMIT or info.flag_bits & 1:
            raise ValueError('oversized or encrypted batch evidence')
        return decode(archive.read(info))


def dispatch(plan):
    source, parent, attempt = context()
    digest, batches = validate_plan(plan, source)
    if plan.get('batch_index') != 0 or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        raise ValueError('large factory selection requires explicit workflow dispatch')
    record = {'sourceRevision': source, 'selectionDigest': digest,
              'parentRunId': parent, 'parentAttempt': attempt, 'batches': [], 'readiness': False}
    path = Path('factory-batch-dispatch.json')
    path.write_text(json.dumps(record, sort_keys=True) + '\n')
    for batch in batches[1:]:
        response = api(f'repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/dispatches', {
            'ref': os.environ['GITHUB_REF_NAME'], 'inputs': {
                'batch_index': str(batch['batch_index']), 'source_revision': source,
                'selection_digest': digest, 'parent_run_id': str(parent),
                'parent_run_attempt': str(attempt), 'cell': os.environ.get('REQUESTED_CELL', ''),
                'selector': os.environ.get('REQUESTED_SELECTOR', ''),
            }})
        child = integer(response.get('workflow_run_id'))
        if child == parent or any(item['runId'] == child for item in record['batches']):
            raise ValueError('dispatch returned a repeated run identity')
        record['batches'].append({'batchIndex': batch['batch_index'], 'runId': child, 'cells': batch['cells']})
        path.write_text(json.dumps(record, sort_keys=True) + '\n')


def verify():
    source, parent, attempt = context()
    digest = os.environ['SELECTION_DIGEST']
    count = integer(os.environ['BATCH_COUNT'])
    plan = artifact(parent, attempt, f'factory-plan-0-attempt{attempt}')
    expected, batches = validate_plan(plan, source)
    if expected != digest or len(batches) != count:
        raise ValueError('parent batch coverage mismatch')
    dispatches = artifact(parent, attempt, f'factory-batch-dispatch-attempt{attempt}')
    if (any(type(dispatches.get(field)) is not int for field in ('parentRunId', 'parentAttempt'))
            or dispatches.get('readiness') is not False):
        raise ValueError('invalid dispatch receipt types')
    if (dispatches.get('sourceRevision') != source or dispatches.get('selectionDigest') != digest
            or dispatches.get('parentRunId') != parent or dispatches.get('parentAttempt') != attempt):
        raise ValueError('dispatch identity mismatch')
    records = dispatches.get('batches')
    if not isinstance(records, list) or len(records) != count - 1:
        raise ValueError('not every planned batch was dispatched')
    children = set()
    for index, record in enumerate(records, 1):
        if (type(record.get('batchIndex')) is not int or record['batchIndex'] != index
                or record.get('cells') != batches[index]['cells']):
            raise ValueError('dispatch selection mismatch')
        child = integer(record['runId'])
        if child == parent or child in children:
            raise ValueError('batch run identity repeated')
        children.add(child)
        run = api(f'repos/{REPOSITORY}/actions/runs/{child}')
        workflow_path = run.get('path', '')
        expected_path = f'.github/workflows/{WORKFLOW}'
        valid_path = workflow_path == expected_path or workflow_path == expected_path + '@' + os.environ['GITHUB_REF_NAME']
        if (type(run.get('id')) is not int or run['id'] != child or run.get('head_sha') != source
                or run.get('event') != 'workflow_dispatch' or run.get('status') != 'completed'
                or run.get('conclusion') != 'success' or not valid_path
                or (run.get('repository') or {}).get('full_name', '').lower() != REPOSITORY
                or (run.get('head_repository') or {}).get('full_name', '').lower() != REPOSITORY):
            raise ValueError('required batch failed, pending, superseded or untrusted')
        child_attempt = integer(run.get('run_attempt'))
        result = artifact(child, child_attempt, f'factory-batch-result-{index}-attempt{child_attempt}')
        if any(type(result.get(field)) is not int for field in
               ('schemaVersion', 'batchIndex', 'batchCount', 'parentRunId', 'parentAttempt', 'runId', 'runAttempt')):
            raise ValueError('batch receipt integer types required')
        if result.get('readiness') is not False:
            raise ValueError('batch receipt cannot establish readiness')
        if result != {'schemaVersion': 1, 'sourceRevision': source, 'selectionDigest': digest,
                      'batchIndex': index, 'batchCount': count, 'parentRunId': parent,
                      'parentAttempt': attempt, 'runId': child, 'runAttempt': child_attempt,
                      'status': 'success', 'readiness': False}:
            raise ValueError('required batch receipt does not match the trusted run')


def write_result():
    source, run, attempt = context()
    digest = os.environ['SELECTION_DIGEST']
    index = integer(os.environ['BATCH_INDEX'], 0)
    count = integer(os.environ['BATCH_COUNT'])
    status = os.environ['BATCH_STATUS']
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or index >= count or count > 64:
        raise ValueError('invalid batch result selection')
    if status not in {'success', 'failure', 'cancelled', 'skipped'}:
        raise ValueError('invalid batch result status')
    document = {'schemaVersion': 1, 'sourceRevision': source,
                'selectionDigest': digest,
                'batchIndex': index,
                'batchCount': count,
                'parentRunId': integer(os.environ['PARENT_RUN_ID']),
                'parentAttempt': integer(os.environ['PARENT_RUN_ATTEMPT']),
                'runId': run, 'runAttempt': attempt,
                'status': os.environ['BATCH_STATUS'], 'readiness': False}
    Path('factory-batch-result.json').write_text(json.dumps(document, sort_keys=True) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['dispatch', 'verify', 'result'])
    parser.add_argument('--plan', type=Path)
    args = parser.parse_args()
    if args.operation == 'dispatch':
        if args.plan is None:
            parser.error('dispatch requires --plan')
        dispatch(decode(args.plan.read_bytes()))
    elif args.operation == 'verify':
        verify()
    else:
        write_result()


if __name__ == '__main__':
    main()
