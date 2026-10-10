#!/usr/bin/env python3
"""Isolated native Alma recipe queue; graph ordering never proves readiness.

CI integration supplies builder(recipe, workdir, signed_repository) and
verifier(recipe, signed_repository). The builder must return prepared RPM
SPECS/SOURCES plus unsigned RPM paths. The verifier must perform actual native
signed-repository resolution/install, returning immutable evidence digests.
This module never admits sibling-cell artifacts or guesses native versions.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

from target_platform import build_context

DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
NAME = re.compile(r'([A-Za-z0-9][A-Za-z0-9+_.-]*)(?:\s*(?:>=|<=|=|>|<)\s*\S+)?\Z')


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def strict_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate identity key')
            result[key] = value
        return result
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('unsafe identity material')
    return json.loads(path.read_text(), object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def expressions(recipe, phase, target, factory):
    section = (recipe.get('dependencies') or {}).get(phase) or {}
    result = list(section.get('common') or []) + list((section.get('targets') or {}).get(target) or [])
    for capability in section.get('capabilities') or []:
        mapped = factory.get('dependency_catalog', {}).get(capability, {}).get(target)
        if not isinstance(mapped, list) or not mapped:
            raise ValueError('unresolved capability: ' + capability)
        result.extend(mapped)
    if any(not isinstance(item, str) or not item or any(ord(char) < 32 for char in item) for item in result):
        raise ValueError('malformed native requirement')
    return sorted(set(result))


def plan(root, target, architecture, source_revision, action_key, packages):
    root = Path(root)
    if target not in {'alma10', 'alma10-kitten'} or architecture not in {'x86_64', 'aarch64'}:
        raise ValueError('unsupported native Alma identity')
    if not SHA.fullmatch(source_revision) or not DIGEST.fullmatch(action_key):
        raise ValueError('immutable source/action identity required')
    if (not isinstance(packages, list) or not packages
            or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+_.-]*', name) for name in packages)
            or len(packages) != len(set(packages))):
        raise ValueError('nonempty unique package queue required')
    factory = yaml.safe_load((root / 'manifests/package-factory.yaml').read_text())
    context = build_context(factory['targets'][target], architecture)
    recipes = {}
    providers = {}
    authored = set()
    for path in sorted((root / 'packages').glob('*/package.yaml')):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('recipe symlink forbidden')
        recipe = yaml.safe_load(path.read_text())
        if (not isinstance(recipe, dict) or not isinstance(recipe.get('name'), str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+_.-]*', recipe['name'])):
            raise ValueError('unsafe recipe identity')
        authored.add(recipe['name'])
        if target not in recipe.get('targets', []):
            continue
        name = recipe['name']
        if name in recipes:
            raise ValueError('duplicate recipe name')
        recipes[name] = (recipe, path)
        for provided in [name, *(recipe.get('provides') or [])]:
            if not isinstance(provided, str):
                raise ValueError('malformed provider expression')
            match = NAME.fullmatch(provided)
            if not match:
                raise ValueError('unsupported provider expression')
            providers.setdefault(match[1], set()).add(name)
    if set(packages) - recipes.keys():
        raise ValueError('requested factory recipe missing for target')
    selected = set(packages)
    dependencies = {}
    native = {}
    records = {}
    pending = sorted(selected)
    while pending:
        name = pending.pop(0)
        recipe, path = recipes[name]
        requirements = {phase: expressions(recipe, phase, target, factory) for phase in ('build', 'runtime')}
        dependencies[name] = set()
        native[name] = []
        for phase, items in requirements.items():
            for expression in items:
                match = NAME.fullmatch(expression)
                candidates = providers.get(match[1], set()) if match else set()
                if len(candidates) > 1:
                    raise ValueError('ambiguous factory provider: ' + expression)
                if candidates:
                    provider = next(iter(candidates))
                    if provider != name:
                        dependencies[name].add(provider)
                        if provider not in selected:
                            selected.add(provider); pending.append(provider); pending.sort()
                else:
                    if match and match[1] in authored:
                        raise ValueError('factory provider unavailable for target: ' + expression)
                    native[name].append({'phase': phase, 'nativeExpression': expression,
                                         'status': 'unresolved-native-provider'})
        records[name] = {'name': name, 'recipe': path.relative_to(root).as_posix(),
                         'recipeDigest': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest(),
                         'requirements': requirements}
    waves = []
    remaining = set(selected)
    done = set()
    while remaining:
        wave = sorted(name for name in remaining if dependencies[name] <= done)
        if not wave:
            raise ValueError('factory provider cycle: ' + ','.join(sorted(remaining)))
        waves.append(wave); done.update(wave); remaining.difference_update(wave)
    material = {'cell': f'tideforge-chain-{target}-{architecture}',
                'target': target, 'architecture': architecture, 'sourceRevision': source_revision,
                'actionKey': action_key, 'requestedRoots': sorted(packages),
                'factoryDigest': 'sha256:' + hashlib.sha256((root / 'manifests/package-factory.yaml').read_bytes()).hexdigest(),
                'baseDigest': context['image'].split('@', 1)[1], 'platform': context['platform'],
                'cpuBaseline': context['cpu_baseline'], 'recipes': [records[name] for name in sorted(records)],
                'dependencies': {name: sorted(items) for name, items in sorted(dependencies.items())}}
    return {'schemaVersion': 1, 'kind': 'alma-recipe-chain', **material,
            'image': context['image'], 'actionKey': action_key, 'queueDigest': digest(material),
            'waves': waves, 'nativeRequirements': native, 'readiness': False,
            'status': 'blocked',
            'requiredVerification': ['native-constraint-resolution', 'signed-artifact-install', 'cpu-baseline-proof']}


def execute(document, *, root, repo, state, meta, work, builder, verifier,
            prepare=None, resume_outputs=None, budget_seconds=16200, elapsed_seconds=0):
    """Run prepared native builds in order, sign/index, then native verify.

    Integration callbacks are trusted CI code, not downloaded artifact fields.
    No callback boolean creates readiness. Snapshot record is build completion
    only; immutable verification observations remain separate from publication.
    """
    if document.get('readiness') is not False:
        raise ValueError('chain cannot claim readiness')
    root, repo, state, meta, work = map(Path, (root, repo, state, meta, work))
    if isinstance(budget_seconds, bool) or not isinstance(budget_seconds, int) or not 1 <= budget_seconds <= 16200:
        raise ValueError('chain budget must respect native soft limit')
    if (isinstance(elapsed_seconds, bool) or not isinstance(elapsed_seconds, (int, float))
            or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0):
        raise ValueError('invalid elapsed chain prephase time')
    started = time.monotonic() - elapsed_seconds
    if not (meta / 'identity.json').is_file() or not (state / 'identity.json').is_file():
        raise ValueError('authenticated same-cell metadata and signing state required')
    expected = plan(root, document['target'], document['architecture'], document['sourceRevision'],
                    document['actionKey'], document['requestedRoots'])
    if document != expected:
        raise ValueError('queue differs from exact authored source plan')
    identity = strict_json(meta / 'identity.json')
    for field in ('cell', 'sourceRevision', 'actionKey', 'target', 'baseDigest', 'cpuBaseline'):
        if identity.get(field) != document[field]:
            raise ValueError('chain identity differs from snapshot identity')
    platform = document['platform'].split('/')
    if identity.get('platform') != {'os': platform[0], 'architecture': platform[1],
                                   'variant': platform[2] if len(platform) == 3 else None}:
        raise ValueError('full platform identity mismatch')
    scripts = root / 'scripts'
    spec = importlib.util.spec_from_file_location('alma_chain_candidate', scripts / 'candidate-rpm-repository.py')
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)
    resume_spec = importlib.util.spec_from_file_location('alma_chain_resume', scripts / 'alma-candidate-resume.py')
    resume = importlib.util.module_from_spec(resume_spec)
    resume_spec.loader.exec_module(resume)
    observations = []
    records = {record['name']: record for record in document['recipes']}
    for wave in document['waves']:
        for name in wave:
            if time.monotonic() - started >= budget_seconds:
                done = {item['name'] for item in observations}
                return {'schemaVersion': 1, 'queueDigest': document['queueDigest'],
                        'observations': observations, 'readiness': False, 'chainComplete': False,
                        'status': 'deferred', 'pendingPackages': sorted(set(records) - done)}
            record = records[name]
            path = root / record['recipe']
            if 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest() != record['recipeDigest']:
                raise ValueError('recipe changed after planning')
            skipped = False
            if prepare is not None:
                prepared = prepare(record, work / name, repo)
                skipped = resume.skip(meta, repo, name, prepared)
                if skipped:
                    completed = [item for item in resume.completed(meta) if item['name'] == name]
                    if len(completed) != 1:
                        raise ValueError('ambiguous authenticated completion')
                    outputs = [repo / item['path'] for item in completed[0]['outputs']]
                    if resume_outputs is None:
                        raise ValueError('resume output consumer required')
                    resume_outputs(record, outputs)
                else:
                    prepared, outputs = builder(record, work / name, repo)
            else:
                prepared, outputs = builder(record, work / name, repo)
            outputs = list(outputs)
            if not outputs or any(Path(output).is_symlink() or not Path(output).is_file()
                                  or Path(output).suffix != '.rpm' for output in outputs):
                raise ValueError('missing or unsafe unsigned build output')
            for output in ([] if skipped else outputs):
                before = candidate.rpm_content(Path(output))
                subprocess.run([sys.executable, str(scripts / 'candidate-rpm-repository.py'), 'install',
                                '--state', str(state), '--repo', str(repo), '--rpm', str(output)], check=True, timeout=600)
                if candidate.rpm_content(repo / Path(output).name) != before:
                    raise ValueError('signing changed native identity or payload')
            subprocess.run([sys.executable, str(scripts / 'candidate-rpm-repository.py'), 'index',
                            '--state', str(state), '--repo', str(repo)], check=True, timeout=600)
            proof = verifier(record, repo)
            if not isinstance(proof, list) or not proof or any(not isinstance(item, str) or not DIGEST.fullmatch(item) for item in proof):
                raise ValueError('native installation/resolution evidence missing')
            args = [sys.executable, str(scripts / 'alma-candidate-resume.py'), 'record', '--meta', str(meta),
                    '--repo', str(repo), '--name', name, '--builddir', str(prepared)]
            for output in outputs:
                args.extend(['--output', Path(output).name])
            subprocess.run(args, check=True, timeout=600)
            observations.append({'name': name, 'reused': skipped, 'evidenceDigests': proof, 'readiness': False})
    return {'schemaVersion': 1, 'queueDigest': document['queueDigest'], 'observations': observations,
            'readiness': False, 'chainComplete': True, 'status': 'completed', 'pendingPackages': []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--architecture', required=True)
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--action-key', required=True)
    parser.add_argument('--package', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--',
                                    'scripts', 'manifests', 'packages'], text=True)
    if head != args.source_revision or dirty:
        raise ValueError('CLI requires exact clean source checkout')
    result = plan(root, args.target, args.architecture,
                  args.source_revision, args.action_key, args.package)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
