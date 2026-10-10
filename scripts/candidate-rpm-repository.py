#!/usr/bin/env python3
"""Sign run-local bootstrap RPMs; this is never production readiness evidence."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['init', 'install', 'index'])
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--rpm', type=Path)
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
    elif args.rpm is None:
        parser.error('install requires --rpm')
    else:
        install(args.rpm.absolute(), repo, state)


if __name__ == '__main__':
    main()
