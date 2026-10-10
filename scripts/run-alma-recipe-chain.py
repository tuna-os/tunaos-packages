#!/usr/bin/env python3
"""CI native Alma chain adapter. Installation observations are not CPU proof."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import time
import os
import shutil
import signal
import uuid
from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('alma_recipe_chain_adapter_core', ROOT / 'scripts/alma-recipe-chain.py')
chain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chain)

# Install from the exact signed local candidate pool; vendor repos remain signed.
# Every explicit build/runtime expression is resolved by native DNF, retaining
# its comparator. Actual automatic RPM runtime dependencies are checked by DNF.
VERIFY = r'''set -eEuo pipefail
dnf -y --setopt=gpgcheck=1 install python3 dnf-plugins-core
python3 - "$TARGET" "$ARCHITECTURE" <<'PY'
import configparser,pathlib,sys
n={'config_opts': {}}
suffix='-aarch64' if sys.argv[2]=='aarch64' else ''
p=pathlib.Path('/factory/mock')/(sys.argv[1]+'-ci'+suffix+'.cfg')
exec(compile(p.read_bytes(),str(p),'exec'),n)
r=configparser.ConfigParser(interpolation=None);r.read_string(n['config_opts']['dnf.conf'])
for name in list(r.sections()):
    if name not in {'main','baseos','appstream','crb','alma-epel-v2','epel-arm'}:r.remove_section(name)
directory=pathlib.Path('/etc/tunaos-chain-repos');directory.mkdir()
with (directory/'native.repo').open('w') as f:r.write(f)
pathlib.Path('/etc/dnf/dnf.conf').write_text('[main]\nreposdir=/etc/tunaos-chain-repos\ngpgcheck=1\nkeepcache=1\nlocalpkg_gpgcheck=1\n')
PY
test -s /candidate-repo/candidate-public.gpg
test -s /candidate-repo/repodata/repomd.xml.asc
rpm --import /candidate-repo/candidate-public.gpg
cat >/etc/tunaos-chain-repos/candidate.repo <<'EOF'
[tunaos-chain-candidate]
name=Exact run-scoped candidate
baseurl=file:///candidate-repo
gpgkey=file:///candidate-repo/candidate-public.gpg
gpgcheck=1
repo_gpgcheck=1
enabled=1
EOF
mapfile -t requirements < /proof/requirements.txt
if ((${#requirements[@]})); then dnf -y --setopt=gpgcheck=1 install "${requirements[@]}"; fi
mapfile -t packages < /proof/packages.txt
((${#packages[@]} > 0))
dnf -y --setopt=gpgcheck=1 --setopt=localpkg_gpgcheck=1 install "${packages[@]}"
dnf check
bash /proof/smoke.sh > /proof/smoke.log 2>&1
rpm -qa --qf '%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SIGPGP:pgpsig}\n' | sort >/proof/installed.tsv
dnf history info >/proof/transaction.txt
dnf repolist -v >/proof/repositories.txt
'''


def run(argv, *, timeout):
    container = len(argv) > 1 and argv[0] in {'docker', 'podman'} and argv[1] == 'run'
    name = 'alma-chain-' + uuid.uuid4().hex if container else None
    command = argv[:2] + ['--name', name] + argv[2:] if container else argv
    try:
        subprocess.run(command, check=True, timeout=timeout)
    except BaseException:
        # Killing the CLI does not kill its daemon-managed container. Complete
        # cleanup before the wrapper signs and banks prior completed packages.
        if container:
            subprocess.run([argv[0], 'rm', '--force', name], timeout=30, check=False)
        raise


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return 'sha256:' + value.hexdigest()


def package_timeout(signum, frame):
    raise TimeoutError('native package operation exceeded bounded in-flight budget')


class Adapter:
    def __init__(self, document, root, work, engine='podman', package_seconds=None, chain_started=None):
        if engine not in {'podman', 'docker'}:
            raise ValueError('unsupported container boundary')
        self.document, self.root, self.work, self.engine = document, Path(root), Path(work), engine
        if document.get('architecture') not in {'x86_64', 'aarch64'}:
            raise ValueError('unsupported native architecture')
        self.outputs = {}
        self.package_seconds = package_seconds
        self.chain_started = chain_started

    def container(self, work, repo):
        # platform v2 is an ISA obligation, not an engine architecture selector.
        architecture = 'amd64' if self.document['architecture'] == 'x86_64' else 'arm64'
        return [self.engine, 'run', '--rm', '--platform', 'linux/' + architecture,
                '--volume', str(self.root.resolve()) + ':/factory:ro',
                '--volume', str((self.root / 'scripts').resolve()) + ':/scripts:ro',
                '--volume', str(Path(work).resolve()) + ':/work:rw',
                '--volume', str(Path(repo).resolve()) + ':/candidate-repo:ro',
                '--env', 'TARGET=' + self.document['target'],
                '--env', 'ARCHITECTURE=' + self.document['architecture'],
                '--env', 'BUILD_IMAGE=' + self.document['image'],
                '--env', 'SOURCE_DATE_EPOCH=' + os.environ.get('SOURCE_DATE_EPOCH', ''),
                '--env', 'TZ=UTC', '--env', 'LANG=C.UTF-8', '--env', 'LC_ALL=C.UTF-8',
                '--env', 'TUNAOS_CANDIDATE_REPO=/candidate-repo']

    def prepare(self, record, work, repo):
        if self.package_seconds is not None:
            # One deadline spans preparation, build, signing, indexing, native
            # installation and completion recording, including reused outputs.
            remaining = self.package_seconds
            if self.chain_started is not None:
                # Stop package work at 5h50m, leaving ten minutes for banking,
                # attestation and uploads within the unchanged six-hour cap.
                remaining = min(remaining, 21000 - (time.monotonic() - self.chain_started))
            if remaining <= 0:
                raise TimeoutError('native chain job reserve reached')
            signal.setitimer(signal.ITIMER_REAL, remaining)
        work = Path(work); work.mkdir(parents=True, exist_ok=False)
        build = work / 'rpmbuild'
        for name in ('BUILD', 'BUILDROOT', 'RPMS', 'SOURCES', 'SPECS', 'SRPMS'):
            (build / name).mkdir(parents=True, exist_ok=True)
        recipe = self.root / record['recipe']
        run([sys.executable, str(self.root / 'scripts/tideforge.py'), 'render', str(recipe),
             '--target', self.document['target'], '--output', str(build / 'SPECS')], timeout=120)
        run([sys.executable, str(self.root / 'scripts/fetch-tideforge-sources.py'), str(recipe),
             str(build / 'SOURCES')], timeout=1800)
        return build

    def restored_outputs(self, record, outputs):
        self.outputs[record['name']] = [Path(path).name for path in outputs]

    def build(self, record, work, repo):
        work = Path(work)
        build = work / 'rpmbuild'
        if not (build / 'SPECS').is_dir() or not (build / 'SOURCES').is_dir():
            raise ValueError('actual prepared inputs missing')
        run(self.container(work, repo) + [self.document['image'], 'bash',
             '/factory/scripts/run-alma-rpm.sh'], timeout=7200)
        outputs = sorted((build / 'RPMS').rglob('*.rpm'))
        if not outputs:
            raise ValueError('native builder produced no RPM')
        evidence = work / 'evidence'
        if evidence.is_symlink() or not evidence.is_dir():
            raise ValueError('actual buildroot evidence missing')
        for path in evidence.rglob('*'):
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                raise ValueError('unsafe buildroot observation')
        # Bank only observations from this actual build; resume retains restored
        # historical observations and performs fresh installation independently.
        bank = Path(repo) / 'buildroots' / record['name']
        if bank.exists():
            shutil.rmtree(bank)
        bank.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(evidence, bank)
        self.outputs[record['name']] = [path.name for path in outputs]
        return build, outputs

    def verify(self, record, repo):
        proof = self.work / 'verification' / record['name']
        proof.mkdir(parents=True, exist_ok=False)
        # Exact signed output filenames prevent a native same-name package from
        # satisfying installation without consuming the authored artifact.
        outputs = sorted(self.outputs.get(record['name'], []))
        if not outputs:
            raise ValueError('signed authored artifacts missing')
        requirements = sorted(set(record['requirements']['build'] + record['requirements']['runtime']))
        (proof / 'requirements.txt').write_text(''.join(item + '\n' for item in requirements))
        (proof / 'packages.txt').write_text(''.join('/candidate-repo/' + item + '\n' for item in outputs))
        recipe_path = self.root / record['recipe']
        if file_digest(recipe_path) != record['recipeDigest']:
            raise ValueError('native smoke recipe differs from authored digest')
        recipe = yaml.safe_load(recipe_path.read_text())
        smoke = recipe.get('verify', {}).get('smoke')
        if not isinstance(smoke, str) or not smoke.strip():
            raise ValueError('authored native smoke command missing')
        (proof / 'smoke.sh').write_text('set -eEuo pipefail\n' + smoke + '\n')
        (proof / 'verify.sh').write_text(VERIFY)
        argv = self.container(proof, repo)
        argv += ['--volume', str(proof.resolve()) + ':/proof:rw', self.document['image'],
                 'bash', '/proof/verify.sh']
        run(argv, timeout=1800)
        observation = {'schemaVersion': 1, 'kind': 'alma-native-install-observation',
            'sourceRevision': self.document['sourceRevision'], 'queueDigest': self.document['queueDigest'],
            'actionKey': self.document['actionKey'], 'baseDigest': self.document['baseDigest'],
            'platform': self.document['platform'], 'cpuBaseline': self.document['cpuBaseline'],
            'recipeDigest': record['recipeDigest'], 'readiness': False,
            'artifacts': [{'name': name, 'digest': file_digest(Path(repo) / name)}
                          for name in outputs],
            'evidence': [{'path': name, 'digest': file_digest(proof / name)}
                         for name in ('installed.tsv', 'transaction.txt', 'repositories.txt', 'verify.sh', 'requirements.txt', 'packages.txt', 'smoke.sh', 'smoke.log')]}
        path = proof / 'observation.json'
        path.write_text(json.dumps(observation, sort_keys=True, indent=2) + '\n')
        return ['sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'repo', 'state', 'meta', 'work', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--engine', choices=['podman', 'docker'], default='podman')
    args = parser.parse_args()
    document = chain.strict_json(args.plan)
    head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain', '--', 'scripts', 'packages', 'manifests', 'mock'], text=True)
    if document['sourceRevision'] != head or dirty:
        raise ValueError('execution requires exact clean authored checkout')
    epoch = subprocess.check_output(['git', '-C', str(ROOT), 'show', '-s', '--format=%ct', head], text=True).strip()
    if not epoch.isdecimal():
        raise ValueError('missing reproducible source timestamp')
    os.environ['SOURCE_DATE_EPOCH'] = epoch
    started = float(os.environ['CHAIN_STARTED_MONOTONIC'])
    elapsed = time.monotonic() - started
    if not math.isfinite(started) or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError('invalid wrapper chain start time')
    adapter = Adapter(document, ROOT, args.work, args.engine, package_seconds=4800,
                      chain_started=started)
    signal.signal(signal.SIGALRM, package_timeout)
    # State is generated locally by the signing utility, never downloaded. Bind
    # the mounted public key to its actual isolated secret-key fingerprint.
    identity = chain.strict_json(args.state / 'identity.json')
    result = subprocess.run(['gpg', '--homedir', str(args.state / 'gnupg'), '--batch',
        '--with-colons', '--list-secret-keys'], check=True, capture_output=True, text=True, timeout=30)
    fingerprints = [line.split(':')[9] for line in result.stdout.splitlines() if line.startswith('fpr:')]
    if identity.get('fingerprint') not in fingerprints:
        raise ValueError('candidate state fingerprint mismatch')
    export = subprocess.run(['gpg', '--homedir', str(args.state / 'gnupg'), '--batch', '--armor',
        '--export', identity['fingerprint']], check=True, capture_output=True, timeout=30).stdout
    public = args.repo / 'candidate-public.gpg'
    if args.repo.is_symlink() or public.is_symlink() or public.read_bytes() != export:
        raise ValueError('mounted candidate key differs from actual signing state')
    for path in args.repo.rglob('*'):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError('unsafe mounted repository entry')
    try:
        result = chain.execute(document, root=ROOT, repo=args.repo, state=args.state,
            meta=args.meta, work=args.work, builder=adapter.build, verifier=adapter.verify,
            prepare=adapter.prepare, resume_outputs=adapter.restored_outputs,
            budget_seconds=int(os.environ.get('CHAIN_BUDGET_SECONDS', '16200')),
            elapsed_seconds=time.monotonic() - started)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
