#!/usr/bin/env python3
"""Sign run-local bootstrap RPMs; this is never production readiness evidence."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import hashlib
import struct
import sys
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile


def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, text=True, **kwargs)


@contextlib.contextmanager
def locked(repo):
    repo.mkdir(parents=True, exist_ok=True)
    with (repo / 'repo.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def identity(state):
    return json.loads((state / 'identity.json').read_text())['fingerprint']


def initialize(state, repo):
    # Refuse restored/unverified seeds instead of signing somebody else's output.
    with locked(repo):
        if list(repo.rglob('*.rpm')):
            raise ValueError('Alma bootstrap requires an empty candidate repository')
        state.mkdir(mode=0o700, parents=True, exist_ok=False)
        home = state / 'gnupg'
        home.mkdir(mode=0o700)
        run('gpg', '--homedir', str(home), '--batch', '--pinentry-mode', 'loopback',
            '--passphrase', '', '--quick-generate-key', 'TunaOS unpromoted bootstrap candidate',
            'rsa3072', 'sign', '1d')
        result = run('gpg', '--homedir', str(home), '--batch', '--with-colons',
                     '--list-secret-keys', stdout=subprocess.PIPE)
        fingerprint = next(line.split(':')[9] for line in result.stdout.splitlines()
                           if line.startswith('fpr:'))
        keys = state / 'keys'
        keys.mkdir(mode=0o755)
        shutil.copyfile(Path(__file__).with_name('alma-rpmbuild-guard.py'), keys / 'alma-rpmbuild-guard.py')
        with (keys / 'candidate-public.gpg').open('w') as output:
            run('gpg', '--homedir', str(home), '--batch', '--armor', '--export', fingerprint,
                stdout=output)
        # Mock mounts file:// repository roots in bootstrap before native DNF
        # starts. Its bind_mount plugin explicitly does not run in bootstrap.
        # Keep the public key in that same mounted repository, never private state.
        shutil.copyfile(keys / 'candidate-public.gpg', repo / 'candidate-public.gpg')
        (keys / 'mock-candidate-policy.cfg').write_text(
            "config_opts['plugin_conf']['root_cache_enable'] = False\n"
            "config_opts['plugin_conf']['bind_mount_enable'] = True\n"
            "_candidate_mounts = config_opts['plugin_conf'].setdefault('bind_mount_opts', {}).setdefault('dirs', [])\n"
            "if ('/keys', '/keys') not in _candidate_mounts: _candidate_mounts.append(('/keys', '/keys'))\n"
            "_candidate_arch = config_opts['target_arch']\n"
            "if _candidate_arch not in ('x86_64', 'aarch64'): raise ValueError('unsupported Alma target CPU')\n"
            "config_opts['rpmbuild_command'] = '/usr/bin/python3 /keys/alma-rpmbuild-guard.py ' + _candidate_arch\n"
            "config_opts['dnf.conf'] = config_opts['dnf.conf'].replace('[local-build]\\n', '[local-build]\\nrepo_gpgcheck=1\\n')\n"
        )
        (keys / 'measure-buildroot.sh').write_text(
            '#!/bin/sh\nset -eu\n'
            'echo "scope=run-local-unpromoted-candidate"\n'
            'echo "productionReady=false"\n'
            'echo "== installed buildroot inventory =="\n'
            "rpm -qa --qf '%{NAME}\\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\\t%{ARCH}\\n' | sort\n"
            'echo "== observed RPM compiler macros =="\n'
            "rpm --eval '%{optflags}\\n%{build_cflags}\\n%{build_cxxflags}\\n%{_target_cpu}\\n%{dist}'\n"
            'echo "== installed compiler identity =="\n'
            'if command -v gcc >/dev/null 2>&1; then gcc --version; else echo "gcc unavailable; compiler evidence incomplete"; fi\n'
            'if command -v clang >/dev/null 2>&1; then clang --version; else echo "clang unavailable"; fi\n'
        )
        (state / 'identity.json').write_text(json.dumps({
            'scope': 'run-local-unpromoted-candidate', 'productionReady': False,
            'fingerprint': fingerprint}, sort_keys=True) + '\n')


def verify_rpm(path, state):
    # An isolated RPM keyring accepts only this run's key, never the host keyring.
    with tempfile.TemporaryDirectory(prefix='candidate-rpmdb-') as directory:
        run('rpm', '--dbpath', directory, '--initdb')
        run('rpm', '--dbpath', directory, '--import', str(state / 'keys/candidate-public.gpg'))
        result = run('rpmkeys', '--dbpath', directory, '--checksig', '--verbose', str(path),
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not re.search(r'^.*Signature[^\n]*:\s*OK\s*$', result.stdout, re.MULTILINE):
            raise ValueError('candidate RPM has no verified signature')


def install(source, repo, state):
    if source.is_symlink() or not source.is_file() or source.suffix != '.rpm':
        raise ValueError('candidate input must be a regular RPM')
    # Signing changes bytes. Stage separately; a failed signature never replaces a candidate.
    with locked(repo), tempfile.TemporaryDirectory(prefix='.candidate-', dir=repo) as directory:
        staged = Path(directory) / source.name
        shutil.copyfile(source, staged)
        run('rpmsign', '--define', '_gpg_name ' + identity(state),
            '--define', '_gpg_path ' + str(state / 'gnupg'),
            '--define', '__gpg /usr/bin/gpg', '--addsign', str(staged),
            env={**os.environ, 'GNUPGHOME': str(state / 'gnupg')})
        verify_rpm(staged, state)
        os.replace(staged, repo / source.name)


def index(repo, state):
    with locked(repo):
        for path in sorted(repo.glob('*.rpm')):
            verify_rpm(path, state)
        # Sign away from the live index. Shared-lock readers see only a complete
        # pair; a signer failure preserves the previous signed metadata.
        with tempfile.TemporaryDirectory(prefix='.candidate-index-', dir=repo) as directory:
            staged = Path(directory)
            run('createrepo_c', '--outputdir', str(staged), str(repo))
            metadata = staged / 'repodata/repomd.xml'
            signature = metadata.with_suffix('.xml.asc')
            run('gpg', '--homedir', str(state / 'gnupg'), '--batch', '--yes', '--armor',
                '--local-user', identity(state), '--output', str(signature), '--detach-sign', str(metadata))
            run('gpg', '--homedir', str(state / 'gnupg'), '--batch', '--verify', str(signature), str(metadata))
            live, backup = repo / 'repodata', staged / 'previous-repodata'
            if live.exists():
                os.replace(live, backup)
            try:
                os.replace(metadata.parent, live)
            except BaseException:
                if backup.exists():
                    os.replace(backup, live)
                raise



def file_digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return 'sha256:' + hasher.hexdigest()


def rpm_content(path):
    """Hash actual RPM main-header/payload bytes, excluding mutable signatures.

    RPM v4 uses a 96-byte lead and an eight-byte padded signature header.
    https://rpm.org/docs/6.0.x/manual/format_v4.html
    """
    size = path.stat().st_size
    if size > 2 * 1024 ** 3:
        raise ValueError('RPM exceeds size limit')
    with path.open('rb') as stream:
        lead = stream.read(96)
        if len(lead) != 96 or lead[:4] != bytes.fromhex('edabeedb'):
            raise ValueError('invalid RPM lead')
        def header_end():
            start = stream.tell()
            header = stream.read(16)
            if len(header) != 16 or header[:8] != bytes.fromhex('8eade80100000000'):
                raise ValueError('invalid RPM header')
            count, length = struct.unpack('>II', header[8:])
            end = start + 16 + count * 16 + length
            if end > size:
                raise ValueError('truncated RPM header')
            return end
        signature_end = header_end()
        stream.seek((signature_end + 7) & ~7)
        main_start = stream.tell()
        payload_start = header_end()
        if payload_start >= size:
            raise ValueError('missing RPM payload')
        stream.seek(main_start)
        content = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            content.update(chunk)
        stream.seek(payload_start)
        payload = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            payload.update(chunk)
    nevra = run('rpm', '-qp', '--qf', '%{NAME}\\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\\t%{ARCH}\\n',
                str(path), stdout=subprocess.PIPE).stdout.strip()
    if not nevra or len(nevra.split('\t')) != 3:
        raise ValueError('invalid queried NEVRA')
    return {'nevra': nevra, 'headerPayloadDigest': 'sha256:' + content.hexdigest(),
            'payloadDigest': 'sha256:' + payload.hexdigest()}


def admit_snapshot(root, manifest, expected_identity, bundle, api_run, repo, state):
    # The subprocess performs cryptographic verification, never a caller boolean.
    with locked(repo):
        if any(path.name not in {'repo.lock', 'repodata', 'candidate-public.gpg'} for path in repo.iterdir()):
            raise ValueError('snapshot admission requires empty candidate repository')
        if not (state / 'identity.json').is_file():
            raise ValueError('fresh candidate state required')
        public_key = repo / 'candidate-public.gpg'
        if public_key.exists() and (public_key.is_symlink() or
                public_key.read_bytes() != (state / 'keys/candidate-public.gpg').read_bytes()):
            raise ValueError('fresh candidate repository public key mismatch')
        predecessor = file_digest(manifest)
        run(sys.executable, str(Path(__file__).with_name('alma-candidate-snapshot.py')), 'verify',
            '--root', str(root), '--manifest', str(manifest), '--identity', str(expected_identity),
            '--bundle', str(bundle), '--api-run', str(api_run), stdout=subprocess.PIPE)
        # Parse strict JSON again through the snapshot helper, after verifier success.
        import importlib.util
        spec = importlib.util.spec_from_file_location('snapshot', Path(__file__).with_name('alma-candidate-snapshot.py'))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        document = module.load(manifest)
        module.validate_manifest(document)
        if module.inventory(root, document['publicKey']['path']) != document['inventory']:
            raise ValueError('snapshot changed after verification')
        if file_digest(manifest) != predecessor:
            raise ValueError('snapshot manifest changed during verification')
        key = root / document['publicKey']['path']
        with tempfile.TemporaryDirectory(prefix='.candidate-admission-', dir=repo) as directory:
            stage = Path(directory)
            gpg_home = stage / 'old-gpg'; gpg_home.mkdir(mode=0o700)
            imported = run('gpg', '--homedir', str(gpg_home), '--batch', '--with-colons',
                           '--import-options', 'show-only', '--import', str(key), stdout=subprocess.PIPE).stdout
            fingerprints = [line.split(':')[9] for line in imported.splitlines() if line.startswith('fpr:')]
            if not fingerprints or fingerprints[0] != document['publicKey']['fingerprint'] or any(
                    line.startswith(('sec:', 'ssb:')) for line in imported.splitlines()):
                raise ValueError('snapshot public key fingerprint mismatch or private material')
            if sum(line.startswith('pub:') for line in imported.splitlines()) != 1:
                raise ValueError('snapshot requires exactly one public key')
            run('gpg', '--homedir', str(gpg_home), '--batch', '--import', str(key))
            metadata = root / 'repodata/repomd.xml'
            signature = root / 'repodata/repomd.xml.asc'
            if not metadata.is_file() or not signature.is_file():
                raise ValueError('signed snapshot metadata required')
            run('gpg', '--homedir', str(gpg_home), '--batch', '--verify', str(signature), str(metadata))
            old_state = stage / 'old-state'; (old_state / 'keys').mkdir(parents=True)
            shutil.copyfile(key, old_state / 'keys/candidate-public.gpg')
            output = stage / 'output'; output.mkdir()
            records = []
            for entry in document['inventory']:
                if not entry['path'].endswith('.rpm'):
                    continue
                source = root / entry['path']
                if source.name != entry['path']:
                    raise ValueError('candidate RPMs must be top-level files')
                if file_digest(source) != entry['digest']:
                    raise ValueError('snapshot RPM changed before admission')
                verify_rpm(source, old_state)
                before = rpm_content(source)
                install(source, output, state)
                admitted = output / source.name
                if file_digest(source) != entry['digest']:
                    raise ValueError('snapshot RPM changed during admission')
                after = rpm_content(admitted)
                if after != before:
                    raise ValueError('RPM content changed while signing')
                records.append({'path': source.name, 'oldDigest': entry['digest'],
                                'newDigest': file_digest(admitted), **before})
            if not records:
                raise ValueError('snapshot has no completed RPMs')
            index(output, state)
            new_digests = {record['path']: record['newDigest'] for record in records}
            completions = [{'name': package['name'], 'inputDigest': package['inputDigest'],
                            'outputs': [{'path': item['path'], 'digest': new_digests[item['path']]}
                                        for item in package['outputs']]}
                           for package in document['completedPackages']]
            receipt = {'schemaVersion': 1, 'productionReady': False,
                       'predecessorSnapshotDigest': predecessor, 'producer': document['identity'],
                       'completedPackages': completions, 'rpms': records}
            (output / 'admission-receipt.json').write_text(json.dumps(receipt, sort_keys=True) + '\n')
            (output / 'repo.lock').unlink()
            backup = stage / 'previous-repodata'
            if (repo / 'repodata').exists():
                os.replace(repo / 'repodata', backup)
            installed = []
            try:
                for path in sorted(output.iterdir()):
                    destination = repo / path.name
                    os.replace(path, destination); installed.append(destination)
            except BaseException:
                for path in reversed(installed):
                    if path.is_dir(): shutil.rmtree(path)
                    else: path.unlink()
                if backup.exists(): os.replace(backup, repo / 'repodata')
                raise

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['init', 'install', 'index', 'admit-snapshot'])
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--rpm', type=Path)
    for option in ('snapshot-root', 'manifest', 'identity', 'bundle', 'api-run'):
        parser.add_argument('--' + option, type=Path)
    args = parser.parse_args()
    for path in (args.state, args.repo):
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError('candidate state and repository must not traverse symlinks')
    state, repo = args.state.resolve(), args.repo.resolve()
    if state == repo or repo in state.parents:
        raise ValueError('private candidate state must be outside repository artifacts')
    if args.operation == 'init':
        initialize(state, repo)
    elif args.operation == 'index':
        index(repo, state)
    elif args.operation == 'admit-snapshot':
        if any(getattr(args, key) is None for key in ('snapshot_root', 'manifest', 'identity', 'bundle', 'api_run')):
            parser.error('admit-snapshot requires snapshot root, manifest, identity, bundle and API run')
        admit_snapshot(args.snapshot_root, args.manifest, args.identity, args.bundle, args.api_run, repo, state)
    elif args.rpm is None:
        parser.error('install requires --rpm')
    else:
        install(args.rpm.absolute(), repo, state)


if __name__ == '__main__':
    main()
