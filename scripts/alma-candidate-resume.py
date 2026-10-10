#!/usr/bin/env python3
"""Bank and resume authenticated native Alma work without publication authority."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import urllib.parse
import re

from github_api import API_VERSION, api
import urllib.error
import zipfile

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('alma_snapshot', HERE / 'alma-candidate-snapshot.py')
snapshot = importlib.util.module_from_spec(spec); spec.loader.exec_module(snapshot)


def command(*args, timeout=120):
    return subprocess.run(list(args), check=True, capture_output=True, text=True, timeout=timeout)


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        json.dump(document, stream, sort_keys=True, separators=(',', ':')); stream.write('\n')
        temporary = Path(stream.name)
    os.replace(temporary, path)


def binding(cell, action_key, run):
    repository = os.environ['GITHUB_REPOSITORY']
    if (run['head_sha'] != os.environ['GITHUB_SHA'] or run['id'] != int(os.environ['GITHUB_RUN_ID']) or
            run['run_attempt'] != int(os.environ['GITHUB_RUN_ATTEMPT']) or
            run['repository']['full_name'] != repository or run['head_repository']['full_name'] != repository):
        raise ValueError('current API producer mismatch')
    platform = cell['platform'].split('/')
    result = {'repository': repository, 'sourceRevision': run['head_sha'], 'workflow': run['path'],
              'signerWorkflow': cell.get('signer_workflow', '.github/workflows/package-factory-cell.yml'),
              'sourceRef': os.environ['GITHUB_REF'], 'runId': run['id'], 'runAttempt': run['run_attempt'],
              'cell': cell.get('base_id') or cell['id'], 'actionKey': action_key,
              'target': cell['target'], 'platform': {'os': platform[0], 'architecture': platform[1],
                                                    'variant': platform[2] if len(platform) == 3 else None},
              'cpuBaseline': cell['cpu_baseline'], 'baseDigest': cell['verify_image'].rsplit('@', 1)[1]}
    snapshot.identity(result)
    return result


def inputs(builddir, identity):
    files = []
    total = 0
    for subdir in ('SPECS', 'SOURCES'):
        root = builddir / subdir
        if root.is_symlink() or not root.is_dir():
            raise ValueError('prepared package inputs missing')
        for path in sorted(root.rglob('*')):
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode): continue
            if not stat.S_ISREG(info.st_mode): raise ValueError('unsafe package input')
            total += info.st_size
            if info.st_size > snapshot.MAX_FILE or total > snapshot.MAX_TOTAL or len(files) >= snapshot.MAX_FILES:
                raise ValueError('package inputs exceed limit')
            hasher = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(1024 * 1024): hasher.update(chunk)
            files.append({'path': path.relative_to(builddir).as_posix(), 'digest': hasher.hexdigest()})
    if not files: raise ValueError('empty package inputs')
    return snapshot.sha256(json.dumps({'identity': {key: value for key, value in identity.items()
                        if key not in {'runId', 'runAttempt'}}, 'files': files}, sort_keys=True).encode())


def completed(meta):
    path = meta / 'completed-packages.json'
    return snapshot.load(path) if path.exists() else []


def record(meta, repo, name, builddir, outputs):
    current = snapshot.load(meta / 'identity.json')
    package = {'name': name, 'inputDigest': inputs(builddir, current), 'outputs': []}
    for output in sorted(outputs):
        snapshot.path_name(output)
        path = repo / output
        info = path.lstat()
        if '/' in output or not output.endswith('.rpm') or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('invalid completed RPM')
        package['outputs'].append({'path': output, 'digest': stream_digest(path)})
    if not package['outputs']: raise ValueError('empty package completion')
    with (meta / 'completion.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        packages = [item for item in completed(meta) if item['name'] != name]
        packages.append(package)
        write(meta / 'completed-packages.json', sorted(packages, key=lambda item: item['name']))


def stream_digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024): hasher.update(chunk)
    return 'sha256:' + hasher.hexdigest()


def skip(meta, repo, name, builddir):
    input_digest = inputs(builddir, snapshot.load(meta / 'identity.json'))
    matches = [item for item in completed(meta) if item['name'] == name and item['inputDigest'] == input_digest]
    if len(matches) != 1: return False
    for output in matches[0]['outputs']:
        path = repo / snapshot.path_name(output['path'])
        if path.is_symlink() or not path.is_file() or stream_digest(path) != output['digest']:
            return False
    return bool(matches[0]['outputs'])



def admit(meta, repo, state, envelope):
    command(sys.executable, str(HERE / 'candidate-rpm-repository.py'), 'admit-snapshot',
            '--state', str(state), '--repo', str(repo), '--snapshot-root', str(envelope / 'payload'),
            '--manifest', str(envelope / 'snapshot.json'), '--identity', str(envelope / 'identity.json'),
            '--bundle', str(envelope / 'bundle.jsonl'), '--api-run', str(envelope / 'api-run.json'), timeout=1800)
    # This receipt was just generated by successful verification and admission.
    receipt = snapshot.load(repo / 'admission-receipt.json')
    if receipt.get('productionReady') is not False:
        raise ValueError('invalid admission receipt')
    write(meta / 'completed-packages.json', receipt['completedPackages'])
    previous = envelope / 'payload/buildroots'
    if previous.exists():
        destination = repo / 'buildroots'; destination.mkdir(exist_ok=True)
        for path in previous.iterdir():
            if path.suffix in {'.txt', '.json'}:
                if path.is_symlink() or not path.is_file(): raise ValueError('unsafe prior observation')
                shutil.copyfile(path, destination / path.name)

def create(meta, repo, destination, chain_complete):
    if destination.exists(): raise ValueError('snapshot destination must be new')
    destination.mkdir(parents=True)
    payload = destination / 'payload'; payload.mkdir()
    try:
        if any(path.is_symlink() for path in (repo, *repo.parents)):
            raise ValueError('unsafe candidate repository')
        for source in sorted(repo.rglob('*')):
            name = snapshot.path_name(source.relative_to(repo).as_posix())
            if source.is_symlink(): raise ValueError('unsafe candidate entry')
            if source.is_dir(): continue
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('unsafe candidate entry')
            # Per-package historical keys are observations, not keyring inputs.
            if name.startswith('buildroots/') and name.endswith('.gpg'): continue
            if name == 'repo.lock': continue
            target = payload / name; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / name, target)
        shutil.copyfile(meta / 'candidate-public.gpg', payload / 'candidate-public.gpg')
        key = snapshot.load(meta / 'candidate-identity.json')
        document = snapshot.create(payload, snapshot.load(meta / 'identity.json'), 'candidate-public.gpg',
                                   key['fingerprint'], completed(meta), chain_complete)
        write(destination / 'snapshot.json', document)
    except BaseException:
        shutil.rmtree(destination)
        raise


def extract(archive, destination):
    with zipfile.ZipFile(archive) as zipped:
        names = set(); total = 0
        for member in zipped.infolist():
            name = member.filename.rstrip('/')
            snapshot.path_name(name)
            mode = member.external_attr >> 16
            if name in names or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError('unsafe or duplicate ZIP entry')
            names.add(name); total += member.file_size
            if len(names) > snapshot.MAX_FILES + 64 or member.file_size > snapshot.MAX_FILE or total > snapshot.MAX_TOTAL:
                raise ValueError('archive exceeds limits')
            if not (name == 'payload' or name.startswith('payload/') or name in {'snapshot.json', 'bundle.jsonl'}):
                raise ValueError('unexpected snapshot envelope entry')
        for member in zipped.infolist():
            path = destination / member.filename
            if member.is_dir(): path.mkdir(parents=True, exist_ok=True); continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(member) as source, path.open('xb') as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None



def validate_artifact_url(url):
    parsed = urllib.parse.urlsplit(url)
    # GitHub's public Actions artifact service uses these Azure storage hosts.
    # Never forward API credentials to a blob or follow a second redirect.
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443) or
            parsed.fragment or not parsed.path.startswith('/actions-results/') or not re.fullmatch(r'productionresultssa[0-9]+\.blob\.core\.windows\.net', parsed.hostname or '')):
        raise ValueError('unapproved public artifact host')

def download(repository, artifact_id, path):
    request = urllib.request.Request('https://api.github.com/repos/' + repository + '/actions/artifacts/' + str(artifact_id) + '/zip',
                headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': API_VERSION})
    try:
        response = urllib.request.build_opener(NoRedirect()).open(request, timeout=120)
    except urllib.error.HTTPError as error:
        if error.code != 302: raise
        location = error.headers['Location']
        validate_artifact_url(location)
        response = urllib.request.build_opener(NoRedirect()).open(location, timeout=120)
    with response, path.open('xb') as output:
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > snapshot.MAX_TOTAL: raise ValueError('archive download exceeds limit')
            output.write(chunk)


def restore(meta, destination):
    current = snapshot.load(meta / 'identity.json')
    repository = current['repository']; prefix = current['cell'] + '-alma-candidate-'
    candidates = []
    discovery_requests = 0
    workflow = urllib.parse.quote(Path(current['workflow']).name, safe='')
    # Limit discovery to this workflow and exact source, not every artifact in
    # a large repository. Artifact names select candidates; provenance authorizes.
    for page in range(1, 6):
        if discovery_requests >= 10: break
        discovery_requests += 1
        runs = api('repos/' + repository + '/actions/workflows/' + workflow + '/runs?head_sha=' +
                   current['sourceRevision'] + '&exclude_pull_requests=true&per_page=30&page=' + str(page))['workflow_runs']
        for producer_run in runs:
            if producer_run.get('head_sha') != current['sourceRevision'] or producer_run.get('path') != current['workflow']:
                continue
            for artifact_page in range(1, 33):
                if discovery_requests >= 10: break
                discovery_requests += 1
                listing = api('repos/' + repository + '/actions/runs/' + str(producer_run['id']) +
                              '/artifacts?per_page=30&page=' + str(artifact_page))['artifacts']
                candidates.extend(item for item in listing if item['name'].startswith(prefix) and not item['expired'] and
                                  item.get('workflow_run', {}).get('head_sha') == current['sourceRevision'] and
                                  item.get('workflow_run', {}).get('id') == producer_run['id'])
                if len(listing) < 30: break
            if len(candidates) >= 5 or discovery_requests >= 10: break
        if len(runs) < 30 or len(candidates) >= 5 or discovery_requests >= 10: break
    candidates.sort(key=lambda item: item['id'], reverse=True)
    for artifact in candidates[:5]:
        with tempfile.TemporaryDirectory(prefix='alma-restore-', dir=meta) as directory:
            stage = Path(directory)
            try:
                download(repository, artifact['id'], stage / 'archive.zip')
                envelope = stage / 'envelope'; envelope.mkdir(); extract(stage / 'archive.zip', envelope)
                document = snapshot.load(envelope / 'snapshot.json')
                snapshot.validate_manifest(document)
                producer = {**current, 'runId': document['identity']['runId'], 'runAttempt': document['identity']['runAttempt']}
                if document['identity'] != producer or artifact['workflow_run']['id'] != producer['runId']:
                    raise ValueError('artifact producer binding differs')
                run = api('repos/' + repository + '/actions/runs/' + str(producer['runId']) + '/attempts/' + str(producer['runAttempt']))
                write(envelope / 'api-run.json', run); write(envelope / 'identity.json', producer)
                command(sys.executable, str(HERE / 'alma-candidate-snapshot.py'), 'verify',
                        '--root', str(envelope / 'payload'), '--manifest', str(envelope / 'snapshot.json'),
                        '--identity', str(envelope / 'identity.json'), '--bundle', str(envelope / 'bundle.jsonl'),
                        '--api-run', str(envelope / 'api-run.json'))
                if destination.exists(): raise ValueError('restore destination must be new')
                shutil.copytree(envelope, destination)
                print('Authenticated Alma predecessor snapshot staged; readiness=false')
                return
            except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile, KeyError) as error:
                print('Alma candidate reuse rejected (' + type(error).__name__ + '); no inputs admitted', file=sys.stderr)
    print('No authenticated matching Alma snapshot; building fresh')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'restore', 'create', 'record', 'skip', 'admit'])
    parser.add_argument('--meta', type=Path, required=True)
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--cell', type=Path)
    parser.add_argument('--action-key')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--name')
    parser.add_argument('--builddir', type=Path)
    parser.add_argument('--output', action='append', default=[])
    parser.add_argument('--chain-complete', action='store_true')
    args = parser.parse_args()
    if args.operation == 'prepare':
        run = api('repos/' + os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' + os.environ['GITHUB_RUN_ID'] + '/attempts/' + os.environ['GITHUB_RUN_ATTEMPT'])
        write(args.meta / 'identity.json', binding(snapshot.load(args.cell), args.action_key, run))
    elif args.operation == 'admit': admit(args.meta, args.repo, args.state, args.destination)
    elif args.operation == 'restore': restore(args.meta, args.destination)
    elif args.operation == 'create': create(args.meta, args.repo, args.destination, args.chain_complete)
    elif args.operation == 'record': record(args.meta, args.repo, args.name, args.builddir, args.output)
    elif args.operation == 'skip': raise SystemExit(0 if skip(args.meta, args.repo, args.name, args.builddir) else 1)


if __name__ == '__main__':
    main()
