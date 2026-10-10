#!/usr/bin/env python3
"""Inventory and authenticate unpromoted Alma candidates; no publication authority."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile

DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')
SHA = re.compile(r'^[0-9a-f]{40}$')
IDENTITY = {'repository', 'sourceRevision', 'workflow', 'sourceRef', 'signerWorkflow', 'runId',
            'runAttempt', 'cell', 'actionKey', 'target', 'platform', 'cpuBaseline', 'baseDigest'}


def reject(message):
    raise ValueError(message)


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            reject('duplicate JSON key: ' + key)
        result[key] = value
    return result


MAX_METADATA = 4 * 1024 * 1024
MAX_FILES = 16384
MAX_FILE = 2 * 1024 ** 3
MAX_TOTAL = 32 * 1024 ** 3


def load(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_METADATA + 1)
    if len(raw) > MAX_METADATA:
        reject('metadata exceeds size limit')
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: reject('nonfinite JSON: ' + value))


def fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        reject('unexpected or missing fields')


def text(value):
    if type(value) is not str or not value or any(c.isspace() for c in value):
        reject('expected nonempty token')


def digest(value):
    if type(value) is not str or not DIGEST.fullmatch(value):
        reject('invalid digest')


def path_name(value):
    text(value)
    path = PurePosixPath(value)
    if value == '.' or path.is_absolute() or str(path) != value or '..' in path.parts or '\\' in value:
        reject('unsafe inventory path')
    if any(part in {'gnupg', 'private-keys-v1.d', 'private-key', 'state'} for part in path.parts):
        reject('private state cannot enter snapshot')
    return value


def sha256(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def identity(value):
    fields(value, IDENTITY)
    for key in IDENTITY - {'runId', 'runAttempt', 'platform'}:
        text(value[key])
    for key in ('runId', 'runAttempt'):
        if type(value[key]) is not int or value[key] <= 0:
            reject('invalid run identity')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value['repository']):
        reject('invalid repository')
    if not SHA.fullmatch(value['sourceRevision']):
        reject('invalid source revision')
    if not value['workflow'].startswith('.github/workflows/') or not value['workflow'].endswith(('.yml', '.yaml')):
        reject('invalid workflow')
    path_name(value['workflow'])
    path_name(value['signerWorkflow'])
    if not value['signerWorkflow'].startswith('.github/workflows/') or not value['signerWorkflow'].endswith(('.yml', '.yaml')):
        reject('invalid signer workflow')
    if not value['sourceRef'].startswith('refs/'):
        reject('invalid source ref')
    digest(value['actionKey']); digest(value['baseDigest'])
    if value['target'] not in {'alma10', 'alma10-kitten'}:
        reject('not a native Alma target')
    fields(value['platform'], {'os', 'architecture', 'variant'})
    if (value['platform'], value['cpuBaseline']) not in [
            ({'os': 'linux', 'architecture': 'amd64', 'variant': 'v2'}, 'x86-64-v2'),
            ({'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'}, 'armv8-a'),
            ({'os': 'linux', 'architecture': 'arm64', 'variant': None}, 'armv8-a')]:
        reject('platform and baseline mismatch')


def inventory(root, public_key=None):
    root = Path(root)
    if any(part.is_symlink() for part in [root, *root.parents]) or not root.is_dir():
        reject('snapshot root must be a real directory')
    result = []
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            relative = path_name(path.relative_to(root).as_posix())
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                reject('snapshot entries must be regular files without links')
            if path.suffix.lower() in {'.key', '.pem', '.p12', '.pfx', '.asc', '.gpg'} and relative != public_key and not relative.endswith('repomd.xml.asc'):
                reject('private key material is forbidden')
            total += info.st_size
            if len(result) >= MAX_FILES or info.st_size > MAX_FILE or total > MAX_TOTAL:
                reject('inventory exceeds size limit')
            hasher = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    hasher.update(chunk)
            result.append({'path': relative, 'size': info.st_size, 'digest': 'sha256:' + hasher.hexdigest()})
    return sorted(result, key=lambda entry: entry['path'])


def validate_manifest(manifest):
    fields(manifest, {'schemaVersion', 'kind', 'productionReady', 'chainComplete',
                      'identity', 'publicKey', 'completedPackages', 'inventory'})
    if type(manifest['schemaVersion']) is not int or manifest['schemaVersion'] != 1:
        reject('unsupported schema')
    if manifest['kind'] != 'alma-candidate-snapshot' or manifest['productionReady'] is not False:
        reject('candidate cannot assert readiness')
    if type(manifest['chainComplete']) is not bool:
        reject('invalid chain completion')
    identity(manifest['identity'])
    key = manifest['publicKey']; fields(key, {'path', 'digest', 'fingerprint'})
    path_name(key['path']); digest(key['digest'])
    if type(key['fingerprint']) is not str or not re.fullmatch(r'[0-9A-F]{40}', key['fingerprint']):
        reject('invalid public key fingerprint')
    if type(manifest['inventory']) is not list or not manifest['inventory']:
        reject('empty inventory')
    entries = {}
    for entry in manifest['inventory']:
        fields(entry, {'path', 'size', 'digest'}); path_name(entry['path']); digest(entry['digest'])
        if type(entry['size']) is not int or entry['size'] < 0 or entry['path'] in entries:
            reject('invalid or duplicate inventory entry')
        entries[entry['path']] = entry
    if list(entries) != sorted(entries):
        reject('inventory must be sorted')
    if key['path'] not in entries or entries[key['path']]['digest'] != key['digest']:
        reject('public key bytes missing from inventory')
    if type(manifest['completedPackages']) is not list:
        reject('invalid package completions')
    names = set(); outputs = set()
    for package in manifest['completedPackages']:
        fields(package, {'name', 'inputDigest', 'outputs'})
        text(package['name']); digest(package['inputDigest'])
        if package['name'] in names or type(package['outputs']) is not list or not package['outputs']:
            reject('invalid or duplicate completed package')
        names.add(package['name'])
        for output in package['outputs']:
            fields(output, {'path', 'digest'}); path_name(output['path']); digest(output['digest'])
            if (output['path'] in outputs or not output['path'].endswith('.rpm') or
                    output['path'] not in entries or entries[output['path']]['digest'] != output['digest']):
                reject('invalid completed package output')
            outputs.add(output['path'])
    if outputs != {name for name in entries if name.endswith('.rpm')}:
        reject('RPM inventory must exactly match completed packages')


def create(root, binding, key_path, fingerprint, completed, chain_complete=False):
    entries = inventory(root, key_path)
    key_entry = next((entry for entry in entries if entry['path'] == key_path), None)
    if key_entry is None:
        reject('public key missing')
    manifest = {'schemaVersion': 1, 'kind': 'alma-candidate-snapshot', 'productionReady': False,
                'chainComplete': chain_complete, 'identity': binding,
                'publicKey': {'path': key_path, 'digest': key_entry['digest'], 'fingerprint': fingerprint},
                'completedPackages': completed, 'inventory': entries}
    validate_manifest(manifest)
    return manifest


def check_provenance(results, snapshot_digest, expected, api_run):
    """Consume successful gh verification output and independently fetched API run data.

    Callers must obtain results from a successful cryptographic verifier, never
    an artifact's claimed verification report. The CLI below enforces that boundary.
    """
    identity(expected)
    if (type(api_run) is not dict or api_run.get('id') != expected['runId'] or
            type(api_run.get('id')) is not int or type(api_run.get('run_attempt')) is not int or
            api_run.get('run_attempt') != expected['runAttempt'] or
            api_run.get('head_sha') != expected['sourceRevision'] or
            api_run.get('path') != expected['workflow'] or
            api_run.get('repository', {}).get('full_name') != expected['repository'] or
            api_run.get('head_repository', {}).get('full_name') != expected['repository']):
        reject('API producer identity mismatch')
    if type(results) is not list or not results:
        reject('missing verified provenance')
    uri = 'https://github.com/' + expected['repository']
    invocation = uri + '/actions/runs/' + str(expected['runId']) + '/attempts/' + str(expected['runAttempt'])
    for result in results:
        try:
            verified = result['verificationResult']; certificate = verified['signature']['certificate']
            statement = verified['statement']
            if (certificate['issuer'] != 'https://token.actions.githubusercontent.com' or
                    certificate['sourceRepositoryURI'] != uri or
                    certificate['sourceRepositoryDigest'] != expected['sourceRevision'] or
                    certificate['sourceRepositoryRef'] != expected['sourceRef'] or
                    certificate['buildSignerURI'] != uri + '/' + expected['signerWorkflow'] + '@' + expected['sourceRef'] or
                    certificate['buildSignerDigest'] != expected['sourceRevision'] or
                    certificate['runInvocationURI'] != invocation):
                continue
            if statement['predicateType'] != 'https://slsa.dev/provenance/v1':
                continue
            if len(statement['subject']) != 1 or statement['subject'][0]['digest'] != {'sha256': snapshot_digest.removeprefix('sha256:')}:
                continue
            return
        except (KeyError, TypeError):
            continue
    reject('verified attestation does not bind snapshot and producer')


def validate(root, manifest, snapshot_bytes, expected, verified, api_run):
    if len(snapshot_bytes) > MAX_METADATA:
        reject('metadata exceeds size limit')
    parsed = json.loads(snapshot_bytes, object_pairs_hook=pairs,
                        parse_constant=lambda value: reject('nonfinite JSON: ' + value))
    if parsed != manifest:
        reject('manifest does not match attested snapshot bytes')
    validate_manifest(manifest)
    if manifest['identity'] != expected:
        reject('snapshot binding mismatch')
    check_provenance(verified, sha256(snapshot_bytes), expected, api_run)
    if inventory(root, manifest['publicKey']['path']) != manifest['inventory']:
        reject('snapshot inventory differs from staged files')
    return {'productionReady': False, 'snapshotDigest': sha256(snapshot_bytes),
            'chainComplete': manifest['chainComplete']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['create', 'verify'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--public-key')
    parser.add_argument('--fingerprint')
    parser.add_argument('--completed-packages', type=Path)
    parser.add_argument('--chain-complete', action='store_true')
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--api-run', type=Path)
    args = parser.parse_args()
    binding = load(args.identity)
    if args.operation == 'create':
        if not args.completed_packages or not args.public_key or not args.fingerprint:
            parser.error('create requires public key, fingerprint and completed packages')
        if args.manifest.resolve().is_relative_to(args.root.resolve()):
            reject('manifest must be outside inventoried root')
        document = create(args.root, binding, args.public_key, args.fingerprint,
                          load(args.completed_packages), args.chain_complete)
        args.manifest.write_text(json.dumps(document, sort_keys=True, separators=(',', ':')) + '\n')
    else:
        if not args.bundle or not args.api_run:
            parser.error('verify requires bundle and independently fetched API run')
        identity(binding)
        with tempfile.TemporaryFile() as output:
            subprocess.run(['gh', 'attestation', 'verify', str(args.manifest), '--repo', binding['repository'],
                                 '--signer-workflow', binding['repository'] + '/' + binding['signerWorkflow'],
                                 '--signer-digest', binding['sourceRevision'], '--source-digest', binding['sourceRevision'],
                                 '--source-ref', binding['sourceRef'], '--bundle', str(args.bundle), '--format', 'json'],
                                check=True, stdout=output, stderr=subprocess.DEVNULL, timeout=120)
            output.seek(0)
            verification_bytes = output.read(MAX_METADATA + 1)
        if len(verification_bytes) > MAX_METADATA:
            reject('verification output exceeds size limit')
        verified = json.loads(verification_bytes, object_pairs_hook=pairs,
                              parse_constant=lambda value: reject('nonfinite JSON: ' + value))
        report = validate(args.root, load(args.manifest), args.manifest.read_bytes(), binding,
                          verified, load(args.api_run))
        print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
