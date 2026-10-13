"""Admit exact experimental desktop/provider inputs; never activate/publish targets."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import sys
import tempfile
import zipfile
import yaml
from github_api import api
import tuna_candidate_inputs as members
import gtk4_candidate
from target_platform import build_context

SCOPE = 'tuna'
DESKTOP_REPO = 'tuna-os/tuna-desktop'
PACKAGE_REPO = 'tuna-os/tunaos-packages'
PRODUCER_SHA = 'ae78b8981bd3439902d9e6f5b3bbb35abb4377ab'
PROVIDER_SHA = '33896640f7b1026d8dd2db020da1fe061415fd85'
PROVIDER_RUN = 38341186906
PROVIDER_REF = 'refs/heads/codex/alma-gtk4-candidate'
WORKFLOW = '.github/workflows/alma-recipe-chain-ci.yml'
MAX_PROVIDER = 64 * 1024 * 1024


def need(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    return 'sha256:' + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(args, timeout=180):
    return subprocess.check_output(args, text=True, timeout=timeout)


def request(path, repository=PACKAGE_REPO):
    need(repository in {PACKAGE_REPO, DESKTOP_REPO} and path.startswith('actions/'), 'unapproved candidate API')
    return api(f'repos/{repository}/{path}')


def download(repository, artifact, destination, limit):
    need(repository in {PACKAGE_REPO, DESKTOP_REPO} and type(artifact.get('id')) is int
         and artifact['id'] > 0 and not artifact.get('expired') and type(artifact.get('size_in_bytes')) is int
         and 0 < artifact['size_in_bytes'] <= limit and re.fullmatch(r'sha256:[0-9a-f]{64}', artifact.get('digest', '')),
         'candidate artifact identity/budget invalid')
    destination = Path(destination)
    need(not destination.is_symlink(), 'candidate ZIP symlink forbidden')
    if not destination.exists():
        def bound():
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        with destination.open('xb') as output:
            subprocess.run(['gh', 'api', '--hostname', 'github.com', f'repos/{repository}/actions/artifacts/{artifact["id"]}/zip'],
                           stdout=output, check=True, timeout=180, preexec_fn=bound)
    need(destination.is_file() and destination.stat().st_size == artifact['size_in_bytes']
         and digest(destination) == artifact['digest'], 'candidate ZIP differs from current API digest')


def extract_provider(archive, destination):
    destination = Path(destination)
    need(not destination.exists(), 'fresh provider extraction required')
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist(); seen = set()
        need(len(entries) <= 16384 and sum(item.file_size for item in entries) <= 256 * 1024 * 1024,
             'provider expanded archive budget exceeded')
        for item in entries:
            path = Path(item.filename); mode = item.external_attr >> 16
            need(item.filename == path.as_posix() + ('/' if item.is_dir() else '')
                 and not path.is_absolute() and '..' not in path.parts and '\\' not in item.filename
                 and item.filename not in seen and stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR),
                 'unsafe provider archive entry')
            seen.add(item.filename)
            need(item.filename in {'snapshot.json', 'bundle.jsonl', 'payload/'} or item.filename.startswith('payload/'),
                 'foreign provider envelope entry')
        destination.mkdir()
        for item in entries:
            path = destination / item.filename
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(zipped.read(item))


def provider_identity(target, arch, factory):
    need(target in {'alma10', 'alma10-kitten'} and arch in {'x86_64', 'aarch64'}, 'native Tuna cell unsupported')
    context = build_context(factory['targets'][target], arch)
    material = {'engine': 'alma-recipe-chain-v1', 'target': target, 'architecture': arch,
                'scope': gtk4_candidate.SCOPE, 'sourceRevision': PROVIDER_SHA, 'requestedRoots': ['gtk4-layer-shell']}
    action = 'sha256:' + hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    platform = context['platform'].split('/')
    return {'repository': PACKAGE_REPO, 'sourceRevision': PROVIDER_SHA, 'workflow': WORKFLOW,
            'signerWorkflow': WORKFLOW, 'sourceRef': PROVIDER_REF, 'runId': PROVIDER_RUN,
            'runAttempt': 1, 'cell': f'tideforge-chain-{target}-{arch}', 'actionKey': action, 'target': target,
            'platform': {'os': platform[0], 'architecture': platform[1], 'variant': platform[2] if len(platform) == 3 else None},
            'cpuBaseline': context['cpu_baseline'], 'baseDigest': context['image'].split('@', 1)[1]}


def signed_rpm(result, signature, fingerprint):
    need('signatures OK' in result and 'NOKEY' not in result and 'NOT OK' not in result
         and signature.startswith('RSA/SHA') and fingerprint[-16:].lower() in signature.lower(),
         'provider RPM unsigned/wrong key')


def admit_provider(root, target, arch, work):
    root, work = Path(root), Path(work)
    factory = yaml.safe_load((root / 'manifests/package-factory.yaml').read_text())
    expected = provider_identity(target, arch, factory)
    run = request(f'actions/runs/{PROVIDER_RUN}')
    need(run['id'] == PROVIDER_RUN and run['head_sha'] == PROVIDER_SHA and run['run_attempt'] == 1
         and run['event'] == 'workflow_dispatch' and run['path'] == WORKFLOW
         and run['head_branch'] == PROVIDER_REF.removeprefix('refs/heads/')
         and run['repository']['full_name'] == PACKAGE_REPO and run['head_repository']['full_name'] == PACKAGE_REPO
         and run['status'] == 'completed' and run['conclusion'] == 'success', 'reviewed provider API run drift')
    jobs = request(f'actions/runs/{PROVIDER_RUN}/jobs?per_page=100')['jobs']
    matches = [job for job in jobs if job['name'] == f'native ({target}, {arch})']
    need(len(matches) == 1 and matches[0]['status'] == 'completed' and matches[0]['conclusion'] == 'success'
         and matches[0]['run_id'] == PROVIDER_RUN and matches[0]['head_sha'] == PROVIDER_SHA,
         'provider native cell did not complete')
    name = f'{expected["cell"]}-alma-candidate-{PROVIDER_RUN}-1-chain'
    matches = [item for item in request(f'actions/runs/{PROVIDER_RUN}/artifacts')['artifacts'] if item['name'] == name]
    need(len(matches) == 1, 'provider artifact missing/ambiguous'); artifact = matches[0]
    need(artifact['workflow_run']['id'] == PROVIDER_RUN and artifact['workflow_run']['head_sha'] == PROVIDER_SHA,
         'provider artifact replay/cross-source')
    work.mkdir(parents=True, exist_ok=True)
    archive = work / 'provider.zip'; download(PACKAGE_REPO, artifact, archive, MAX_PROVIDER)
    envelope = work / 'provider'
    if not envelope.exists(): extract_provider(archive, envelope)
    (work / 'provider-identity.json').write_text(json.dumps(expected))
    (work / 'provider-api-run.json').write_text(json.dumps(run))
    verified = json.loads(command([sys.executable, str(root / 'scripts/alma-candidate-snapshot.py'), 'verify',
            '--root', str(envelope / 'payload'), '--manifest', str(envelope / 'snapshot.json'),
            '--identity', str(work / 'provider-identity.json'), '--bundle', str(envelope / 'bundle.jsonl'),
            '--api-run', str(work / 'provider-api-run.json')]))
    need(verified['chainComplete'] is True and verified['productionReady'] is False, 'provider incomplete/overclaimed')
    manifest = json.loads((envelope / 'snapshot.json').read_text())
    need({row['name'] for row in manifest['completedPackages']} == {'ninja-build', 'gtk4-layer-shell'}, 'wrong provider completion queue')
    payload = envelope / 'payload'; public = payload / manifest['publicKey']['path']
    public_home = work / 'public-gpg'; public_home.mkdir(mode=0o700, exist_ok=True)
    keys = command(['gpg', '--homedir', str(public_home), '--batch', '--show-keys', '--with-colons', str(public)])
    fingerprints = [line.split(':')[9] for line in keys.splitlines() if line.startswith('fpr:')]
    need(fingerprints == [manifest['publicKey']['fingerprint']], 'provider public fingerprint drift')
    command(['gpg', '--homedir', str(public_home), '--batch', '--import', str(public)])
    command(['gpg', '--homedir', str(public_home), '--batch', '--verify', str(payload / 'repodata/repomd.xml.asc'), str(payload / 'repodata/repomd.xml')])
    rpm_db = work / 'rpmdb'; rpm_db.mkdir(exist_ok=True)
    command(['rpmkeys', '--dbpath', str(rpm_db), '--import', str(public)])
    for package in manifest['completedPackages']:
        for output in package['outputs']:
            path = payload / output['path']; need(digest(path) == output['digest'], 'provider signed RPM changed')
            result = command(['rpmkeys', '--dbpath', str(rpm_db), '--checksig', str(path)])
            signature = command(['rpm', '--dbpath', str(rpm_db), '-qp', '--qf', '%{RSAHEADER:pgpsig}', str(path)])
            signed_rpm(result, signature, manifest['publicKey']['fingerprint'])
    bank = payload / 'buildroots/gtk4-layer-shell'
    tests = gtk4_candidate.admit_tests(bank / 'gtk4-provider', gtk4_candidate.SOURCE_SHA)
    proof = json.loads((bank / 'gtk4-provider-admission.json').read_text())
    spec = importlib.util.spec_from_file_location('tuna_provider_chain', root / 'scripts/alma-recipe-chain.py')
    chain = importlib.util.module_from_spec(spec); spec.loader.exec_module(chain)
    plan = chain.plan(root, target, arch, PROVIDER_SHA, expected['actionKey'], ['gtk4-layer-shell'],
                      candidate_scope=gtk4_candidate.SCOPE)
    canonical = root / 'packages/gtk4-layer-shell/package.yaml'
    projected = gtk4_candidate.project(yaml.safe_load(canonical.read_text()), target)
    projected_digest = 'sha256:' + hashlib.sha256(json.dumps(projected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    need(proof['schemaVersion'] == 1 and proof['kind'] == 'gtk4-candidate-upstream-tests'
         and proof['queueDigest'] == plan['queueDigest'] and proof['recipeDigest'] == digest(canonical) and proof['candidateRecipeDigest'] == projected_digest
         and proof['sourceRevision'] == PROVIDER_SHA and proof['actionKey'] == expected['actionKey']
         and proof['baseDigest'] == expected['baseDigest'] and proof['platform'] == build_context(factory['targets'][target], arch)['platform']
         and proof['published'] is False and proof['readiness'] is False and proof['hardwareQualified'] is False
         and proof['evidence'] == tests, 'provider test source/cell drift')
    return {'identity': expected, 'snapshot_digest': verified['snapshotDigest'], 'artifact_digest': artifact['digest'],
            'artifact_id': artifact['id'], 'envelope': str(envelope), 'api_run': str(work / 'provider-api-run.json'),
            'expected_identity': str(work / 'provider-identity.json'), 'tests': proof}


def cells(factory):
    result = []
    for consumer, target in {'albacore': 'alma10', 'yellowfin': 'alma10-kitten'}.items():
        config = factory['targets'][target]; adapter = factory['consumer_adapters'][consumer]
        for arch in ('x86_64', 'aarch64'):
            image = config['probe_images'][arch]
            platform = next(value for value, value_arch in adapter['architectures'].items() if value_arch == arch)
            result.append({'consumer': consumer, 'target': target, 'arch': arch, 'base_image': image,
                           'base_reference': image, 'platform': platform})
    return result


def admit_desktop(options, root, target, arch, work):
    need(set(options) == {'source_run', 'source_sha', 'producer_run', 'producer_sha', 'artifact', 'helper_checkout'}, 'unexpected desktop input fields')
    need(options['producer_sha'] == PRODUCER_SHA and re.fullmatch(r'[0-9a-f]{40}', options['source_sha'])
         and all(type(options[name]) is int and options[name] > 0 for name in ('source_run', 'producer_run', 'artifact')),
         'reviewed desktop/producer identity required')
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    receipt = members.independently_admit(options['helper_checkout'], PRODUCER_SHA,
                                         options['source_run'], options['producer_run'], options['artifact'])
    need(receipt['source_sha'] == options['source_sha'] and receipt['source']['run_id'] == options['source_run'], 'desktop source replay/drift')
    artifact = request(f'actions/artifacts/{options["artifact"]}', DESKTOP_REPO)
    need(artifact['workflow_run']['id'] == options['producer_run'] and artifact['workflow_run']['head_sha'] == PRODUCER_SHA
         and artifact['digest'] == 'sha256:' + receipt['admission']['artifact_sha256']
         and artifact['size_in_bytes'] == receipt['admission']['artifact_size'], 'desktop artifact source drift')
    archive = work / 'desktop.zip'; download(DESKTOP_REPO, artifact, archive, members.LIMIT)
    factory = Path(root) / 'manifests/package-factory.yaml'
    assembly = work / 'assembly'
    # Every execution re-admits API identities and checks immutable local members.
    # Generated outputs are disposable, never accepted as prior claimed proof.
    if assembly.exists():
        import shutil
        need(not assembly.is_symlink(), 'unsafe old native assembly'); shutil.rmtree(assembly)
    result = members.assemble(Path(root) / 'packages/tuna-desktop/package.yaml', factory,
                cells(yaml.safe_load(factory.read_text())), archive, assembly, receipt, native=True, selected_cell=(target, arch))
    recipe = yaml.safe_load((assembly / 'recipe/package.yaml').read_text())
    consumer = {'alma10': 'albacore', 'alma10-kitten': 'yellowfin'}[target]
    return {'recipe': recipe, 'recipe_digest': members.digest((assembly / 'recipe/package.yaml').read_bytes()),
            'assembly': str(assembly / f'{consumer}-{target}-{arch}/rpmbuild'), 'source': receipt,
            'minimum_rust_version': result['minimum_rust_version']}


class Inputs:
    """In-process admitted material; CLI constructors always recheck remote proofs."""
    def __init__(self, options, desktop, provider):
        self.options, self.desktop, self.provider = options, desktop, provider

    @property
    def recipe(self):
        return self.desktop['recipe']

    def binding(self):
        provider = {name: value for name, value in self.provider.items()
                    if name not in {'envelope', 'api_run', 'expected_identity'}}
        return {'desktop': self.desktop['source'], 'provider': provider,
                'recipe_sha256': self.desktop['recipe_digest'],
                'minimum_rust_version': self.desktop['minimum_rust_version']}

    def admit_into(self, root, repo, state):
        provider = self.provider; envelope = Path(provider['envelope'])
        # Dependency admission remains fresh/isolated even when a native Tuna
        # attempt resumes its own separately authenticated completed packages.
        import shutil
        with tempfile.TemporaryDirectory(prefix='tuna-provider-import-') as temporary:
            staged = Path(temporary) / 'repository'; staged.mkdir()
            command([sys.executable, str(Path(root) / 'scripts/candidate-rpm-repository.py'), 'admit-snapshot',
                     '--state', str(state), '--repo', str(staged), '--snapshot-root', str(envelope / 'payload'),
                     '--manifest', str(envelope / 'snapshot.json'), '--identity', provider['expected_identity'],
                     '--bundle', str(envelope / 'bundle.jsonl'), '--api-run', provider['api_run']], timeout=1800)
            spec = importlib.util.spec_from_file_location('tuna_dependency_content', Path(root) / 'scripts/candidate-rpm-repository.py')
            signing = importlib.util.module_from_spec(spec); spec.loader.exec_module(signing)
            for artifact in sorted(staged.glob('*.rpm')):
                existing = Path(repo) / artifact.name
                if existing.exists():
                    need(not existing.is_symlink() and signing.rpm_content(existing) == signing.rpm_content(artifact),
                         'resumed native provider payload differs')
                else:
                    command([sys.executable, str(Path(root) / 'scripts/candidate-rpm-repository.py'), 'install',
                             '--state', str(state), '--repo', str(repo), '--rpm', str(artifact)], timeout=600)
            command([sys.executable, str(Path(root) / 'scripts/candidate-rpm-repository.py'), 'index',
                     '--state', str(state), '--repo', str(repo)], timeout=600)
            bank = Path(repo) / 'buildroots/tuna-provider-input'
            if bank.exists(): shutil.rmtree(bank)
            bank.mkdir(parents=True)
            (bank / 'provider-input.json').write_text(json.dumps(self.binding()['provider'], sort_keys=True)+'\n')


def prepare(options, root, target, arch, work):
    need(target in {'alma10', 'alma10-kitten'} and arch in {'x86_64', 'aarch64'}, 'native Tuna cell unsupported')
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    desktop = admit_desktop(options, root, target, arch, work / 'desktop')
    provider = admit_provider(root, target, arch, work / 'provider')
    return Inputs(options, desktop, provider)


def from_file(path, root, target, arch):
    path = Path(path)
    need(path.is_file() and not path.is_symlink() and path.stat().st_size <= 65536, 'unsafe desktop input options')
    def pairs(values):
        result = {}
        for key, value in values:
            need(key not in result, 'duplicate native input option')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('nonfinite native input option')
    options = json.loads(path.read_text(), object_pairs_hook=pairs, parse_constant=nonfinite)
    need(isinstance(options, dict), 'native input options must be an object')
    return prepare(options, root, target, arch, path.parent / 'admitted-tuna')
