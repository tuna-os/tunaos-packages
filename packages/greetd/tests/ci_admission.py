"""Fresh API/Git admission for opt-in, nonpublishing native greetd controls."""
import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import urllib.request
from pathlib import Path

REPO = 'tuna-os/tunaos-packages'
WORKFLOW = '.github/workflows/validate-package-factory.yml'
SOURCES = [WORKFLOW, 'packages/greetd/0001-Owned-authentication-worker-retirement.patch',
           'packages/greetd/tests/pam_retirement_fixture.c',
           'packages/greetd/tests/greetd_native_proof.py',
           'packages/greetd/tests/native_daemon_owner.py',
           'packages/greetd/tests/retirement_peer.py',
           'packages/greetd/tests/ci_admission.py', 'packages/greetd/tests/build_native_candidate.py']


def api(path):
    token = os.environ.get('GH_TOKEN')
    if not token:
        raise ValueError('read-only GitHub token required')
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + '/' + path,
                                    headers={'Authorization': 'Bearer ' + token,
                                             'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.geturl().split('/')[2] != 'api.github.com':
            raise ValueError('unexpected API authority')
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('API response exceeds bound')
    return json.loads(raw)


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'safe.directory=' + str(root), '-C', str(root), *args], timeout=30).decode().strip()


def current(root, architecture):
    root = Path(root).resolve(strict=True)
    if architecture not in ('x86_64', 'aarch64') or platform.machine() != architecture:
        raise ValueError('native architecture not admitted')
    head = os.environ.get('GITHUB_SHA', '')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    ref = os.environ.get('GITHUB_REF', '')
    if (os.environ.get('GITHUB_REPOSITORY') != REPO
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or os.environ.get('GITHUB_JOB') != 'greetd-retirement'
            or not re.fullmatch('[0-9a-f]{40}', head)
            or not run_id.isdecimal() or int(run_id) <= 0
            or not attempt.isdecimal() or int(attempt) <= 0
            or not ref.startswith('refs/heads/')
            or os.environ.get('GITHUB_WORKFLOW_REF') != REPO + '/' + WORKFLOW + '@' + ref
            or Path(os.environ.get('GITHUB_WORKSPACE', '/missing')).resolve() != root
            or git(root, 'rev-parse', 'HEAD') != head
            or git(root, 'status', '--porcelain', '--untracked-files=all')):
        raise ValueError('exact clean reviewed workflow checkout required')
    run = api('actions/runs/' + run_id)
    if (type(run.get('id')) is not int or type(run.get('run_attempt')) is not int
            or run.get('id') != int(run_id) or run.get('run_attempt') != int(attempt)
            or run.get('head_sha') != head or run.get('head_branch') != ref[len('refs/heads/'):]
            or run.get('path') != WORKFLOW or run.get('event') != 'workflow_dispatch'
            or run.get('status') not in ('queued', 'in_progress') or run.get('conclusion') is not None
            or run.get('repository', {}).get('full_name') != REPO
            or run.get('head_repository', {}).get('full_name') != REPO):
        raise ValueError('fresh API run/source/event differs')
    jobs = api('actions/runs/' + run_id + '/attempts/' + attempt + '/jobs?per_page=100')
    if type(jobs.get('total_count')) is not int or not 0 < jobs['total_count'] <= 100:
        raise ValueError('bounded complete job list required')
    if len(jobs.get('jobs', [])) != jobs['total_count']:
        raise ValueError('partial jobs page refused')
    owned = [job for job in jobs['jobs'] if job.get('name') == 'greetd-retirement-' + architecture]
    prerequisites = [job for job in jobs['jobs'] if job.get('name') == 'validate']
    if len(owned) != 1 or len(prerequisites) != 1:
        raise ValueError('unique native job and validator prerequisite required')
    for job in [*owned, *prerequisites]:
        if (type(job.get('id')) is not int or job['id'] <= 0
                or type(job.get('run_id')) is not int or type(job.get('run_attempt')) is not int
                or job.get('run_id') != int(run_id) or job.get('run_attempt') != int(attempt)
                or job.get('head_sha') != head):
            raise ValueError('job source/attempt differs')
    if owned[0].get('status') != 'in_progress' or owned[0].get('conclusion') is not None:
        raise ValueError('actual native job must be executing')
    if prerequisites[0].get('status') != 'completed' or prerequisites[0].get('conclusion') != 'success':
        raise ValueError('validator prerequisite did not succeed')
    commit = api('git/commits/' + head)
    tree = git(root, 'rev-parse', 'HEAD^{tree}')
    if commit.get('sha') != head or commit.get('tree', {}).get('sha') != tree:
        raise ValueError('exact API tree differs')
    measured = {}
    for name in SOURCES:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            raise ValueError('bounded regular executing source required')
        data = path.read_bytes()
        remote = api('contents/' + name + '?ref=' + head)
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if (remote.get('type') != 'file' or remote.get('path') != name
                or remote.get('encoding') != 'base64' or remote.get('size') != len(data)
                or remote.get('sha') != blob or base64.b64decode(remote.get('content', '')) != data):
            raise ValueError('executing source bytes differ from API Git blob')
        measured[name] = hashlib.sha256(data).hexdigest()
    return {'source_commit': head, 'source_tree': tree, 'run_id': int(run_id),
            'run_attempt': int(attempt), 'job_id': owned[0]['id'],
            'architecture': architecture, 'source_sha256': measured,
            'candidate_only': True, 'release_qualified': False, 'publication': False}
