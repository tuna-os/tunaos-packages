"""Consumer bindings scope factory scheduling and cache inputs to exact targets."""
from __future__ import annotations
import argparse
import copy
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import factory_contract


def load(filename, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


planner = load('plan-package-factory.py', 'consumer_factory_planner')
cache = load('tideforge-action-cache.py', 'consumer_factory_cache')
DIGEST = 'sha256:' + 'a' * 64
REVISION = '1' * 40
TARGET = {'variant': 'albacore', 'flavor': 'cosmic', 'platform': 'linux/amd64/v2',
          'cpuBaseline': 'x86-64-v2', 'hardwareScope': 'generic'}


def binding(**updates):
    value = {'target': TARGET, 'sourceRevision': REVISION, 'contractDigest': DIGEST,
             'baseDigest': DIGEST, 'baseReference': 'quay.io/example/base@' + DIGEST,
             'approvedSources': [{'id': 'factory', 'url': 'https://repo.tunaos.org/rpm/',
                                  'signingIdentity': 'tunaos-factory', 'snapshotDigest': DIGEST}]}
    value.update(updates)
    return value


def factory():
    return {'targets': {'el10': {'format': 'rpm', 'architectures': ['x86_64', 'aarch64']}},
            'consumer_adapters': {'albacore': {'target': 'el10', 'manager': 'dnf',
                                             'architectures': {'linux/amd64/v2': 'x86_64', 'linux/arm64': 'aarch64'},
                                             'cpuBaselines': ['x86-64-v2', 'armv8-a']}}}


def optional_args(**updates):
    values = {'consumer_contracts': None, 'consumer_root': None, 'consumer_revision': None,
              'consumer_required_targets': None, 'consumer_provider_catalog': None, 'consumer_plan_output': None}
    values.update(updates)
    return argparse.Namespace(**values)


def test_default_planner_does_not_require_consumer_bundle():
    assert planner.consumer_factory_plan(optional_args(), []) is None


@pytest.mark.parametrize('field', ['consumer_contracts', 'consumer_root', 'consumer_revision',
                                   'consumer_required_targets', 'consumer_provider_catalog', 'consumer_plan_output'])
def test_partial_consumer_inputs_fail_closed(field):
    with pytest.raises(ValueError, match='consumer'):
        planner.consumer_factory_plan(optional_args(**{field: 'one-input'}), [])


def test_reverse_dependency_closure_excludes_unrelated_targets_and_arches():
    cells = [{'id': name, 'target': target, 'architecture': arch} for name, target, arch in [
        ('a', 'el10', 'x86_64'), ('b', 'el10', 'x86_64'), ('c', 'el10', 'x86_64'),
        ('a-arm', 'el10', 'aarch64'), ('other-target', 'ubuntu', 'amd64')]]
    row = {'targetKey': 'albacore:cosmic:linux/amd64/v2', **binding()}
    report = {'dependencyCells': {'b': ['a'], 'c': [], 'a-arm': [], 'other-target': []},
              'cellBindings': {'a': [row['targetKey']], 'b': [row['targetKey']]}, 'consumers': [row]}
    selected = planner.bind_consumer_cells(cells, [cells[0]], report)
    assert [cell['id'] for cell in selected] == ['a', 'b']
    assert selected[0]['consumer_bindings'] == [binding()]
    assert selected[1]['consumer_bindings'] == [binding()]
    assert all('consumer_bindings' not in cell for cell in cells)


def test_declared_recipes_preserve_native_dependencies_and_never_claim_readiness(tmp_path):
    recipe = tmp_path / 'packages/demo/package.yaml'
    recipe.parent.mkdir(parents=True)
    recipe.write_text('''name: demo
version: "1.2.3"
dependencies:
  build:
    common: ["compiler >= 2"]
    targets: {el10: ["extra-devel = 1.4"]}
    capabilities: [cmake]
  runtime:
    common: ["libdemo >= 1.2"]
''')
    configuration = factory()
    configuration['dependency_catalog'] = {'cmake': {'el10': ['cmake >= 3.20']}}
    cells = [{'id': 'demo-el10-x86_64', 'engine': 'tideforge', 'recipe': 'packages/demo/package.yaml',
              'format': 'rpm', 'target': 'el10', 'architecture': 'x86_64'},
             {'id': 'native', 'engine': 'build-chain'}]
    providers = planner.declared_consumer_providers(tmp_path, configuration, cells)['providers']
    assert len(providers) == 1
    assert providers[0]['nativeExpression'] == 'demo'
    assert providers[0]['declaredVersion'] == '1.2.3'
    assert providers[0]['cpuBaseline'] == 'x86-64-v2'
    assert providers[0]['buildDependencies'] == [{'nativeExpression': 'compiler >= 2'},
                                               {'nativeExpression': 'extra-devel = 1.4'},
                                               {'nativeExpression': 'cmake >= 3.20'}]
    assert providers[0]['runtimeDependencies'] == [{'nativeExpression': 'libdemo >= 1.2'}]
    assert providers[0]['readiness'] is False


def cache_fixture(tmp_path):
    recipe = tmp_path / 'packages/demo/package.yaml'
    recipe.parent.mkdir(parents=True)
    recipe.write_text('name: demo\ndependencies: {}\n')
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    for name in ['tideforge.py', 'assemble-deb-source-tree.py', 'build-chain.sh',
                 'run-package-factory-cell.sh', 'fetch-tideforge-sources.py', 'import-fedora-distgit.py']:
        (scripts / name).write_text('# fake boundary\n')
    manifest = tmp_path / 'factory.yaml'
    manifest.write_text(yaml.safe_dump(factory()))
    bindings = tmp_path / 'bindings.json'
    bindings.write_text(json.dumps({'bindings': [binding()]}))
    return argparse.Namespace(root=str(tmp_path), recipe=str(recipe), factory=str(manifest),
                              target='el10', arch='x86_64', image='registry.example/build@' + DIGEST,
                              source_date_epoch=1700000000, dependency_key=[], consumer_bindings=str(bindings))


def test_action_key_binds_target_local_contract_source_base_and_snapshot(tmp_path):
    args = cache_fixture(tmp_path)
    initial = cache.action_key(cache.action_inputs(args))
    for field in ['contractDigest', 'sourceRevision', 'baseDigest', 'approvedSources']:
        value = copy.deepcopy(binding())
        if field == 'sourceRevision':
            value[field] = '2' * 40
        elif field == 'approvedSources':
            value[field][0]['snapshotDigest'] = 'sha256:' + 'b' * 64
        else:
            value[field] = 'sha256:' + 'b' * 64
            if field == 'baseDigest':
                value['baseReference'] = 'quay.io/example/base@' + value[field]
        pathlib.Path(args.consumer_bindings).write_text(json.dumps({'bindings': [value]}))
        assert cache.action_key(cache.action_inputs(args)) != initial
    pathlib.Path(args.consumer_bindings).write_text(json.dumps({'bindings': [binding()]}))
    configuration = factory()
    configuration['consumer_adapters']['unrelated'] = {'target': 'another', 'cpuBaselines': ['other']}
    pathlib.Path(args.factory).write_text(yaml.safe_dump(configuration))
    assert cache.action_key(cache.action_inputs(args)) == initial


@pytest.mark.parametrize('field,value', [('sourceRevision', 'main'), ('contractDigest', 'sha256:abc'),
                                        ('baseDigest', 'sha256:abc'), ('approvedSources', None)])
def test_malformed_bindings_are_rejected(field, value):
    with pytest.raises(ValueError):
        factory_contract.consumer_binding_inputs([binding(**{field: value})], factory(), 'el10', 'x86_64')


@pytest.mark.parametrize('field,value', [('platform', 'linux/arm64'), ('cpuBaseline', 'x86-64-v3'),
                                        ('variant', 'skipjack')])
def test_another_target_architecture_or_baseline_is_rejected(field, value):
    value = binding(target={**TARGET, field: value})
    with pytest.raises(ValueError):
        factory_contract.consumer_binding_inputs([value], factory(), 'el10', 'x86_64')


def test_workflow_passes_consumer_bindings_to_both_cache_engines():
    workflow = yaml.safe_load((ROOT / '.github/workflows/package-factory-cell.yml').read_text())
    scripts = [step.get('run', '') for job in workflow['jobs'].values() for step in job.get('steps', [])]
    identity = next(body for body in scripts if 'consumer_args=()' in body)
    assert 'consumer_args=(--consumer-bindings "$consumer_file")' in identity
    assert identity.count('"${consumer_args[@]}"') == 2
    assert 'tideforge-action-cache.py key' in identity
    assert 'tideforge-action-cache.py native-key' in identity


def test_legacy_default_cli_retains_current_local_cells():
    run = subprocess.run([sys.executable, str(ROOT / 'scripts/plan-package-factory.py'), '--root', str(ROOT)],
                         cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    document = json.loads(run.stdout)
    assert 'consumerPlan' not in document
    expected = {cell['id'] for cell in planner.all_cells(ROOT)}
    assert document['selection_count'] == len(expected)
    assert {cell['id'] for cell in document['selection_inventory']} == expected
    source = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, check=True,
                            capture_output=True, text=True).stdout.strip()
    assert document['source_revision'] == source
    complete = []
    for index in range(document['batch_count']):
        if index:
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/plan-package-factory.py'),
                '--root', str(ROOT), '--batch-index', str(index),
                '--selection-digest', document['selection_digest']], cwd=ROOT, capture_output=True, text=True)
            assert run.returncode == 0, run.stderr
            batch = json.loads(run.stdout)
        else:
            batch = document
        assert 'consumerPlan' not in batch
        assert batch['selection_digest'] == document['selection_digest']
        assert batch['source_revision'] == source
        actual = [cell for matrix in batch['matrices']
                  for cell in json.loads(matrix)['include'] if 'base_id' not in cell]
        assert batch['count'] == len(actual)
        assert {cell['id'] for cell in actual} == set(document['planned_batches'][index]['cells'])
        complete.extend(cell['id'] for cell in actual)
    assert set(complete) == expected
    assert len(complete) == len(expected)



@pytest.mark.parametrize('target', [{**TARGET, 'flavor': '../secret'},
                                   {**TARGET, 'hardwareScope': 'apple-silicon'},
                                   {**TARGET, 'flavor': 'gnome-asahi'},
                                   {**TARGET, 'hardwareScope': 'unknown'}])
def test_cache_binding_requires_canonical_hardware_identity(target):
    with pytest.raises(ValueError):
        factory_contract.consumer_binding_inputs([binding(target=target)], factory(), 'el10', 'x86_64')


@pytest.mark.parametrize('sources', [[{'token': 'secret'}],
                                    [{'id': 'factory', 'url': 'https://repo.tunaos.org/rpm/',
                                      'signingIdentity': 'factory', 'snapshotDigest': 'sha256:abc'}]])
def test_cache_binding_source_snapshot_shape_is_validated(sources):
    with pytest.raises(ValueError):
        factory_contract.consumer_binding_inputs([binding(approvedSources=sources)], factory(), 'el10', 'x86_64')


@pytest.mark.parametrize('missing', ['consumer_contracts', 'consumer_root', 'consumer_revision',
                                     'consumer_required_targets'])
def test_each_required_bundle_input_is_mandatory(missing):
    inputs = {'consumer_contracts': 'contracts.json', 'consumer_root': 'consumer-root',
              'consumer_revision': REVISION, 'consumer_required_targets': 'required-targets.json'}
    inputs[missing] = None
    with pytest.raises(ValueError, match='supplied together'):
        planner.consumer_factory_plan(optional_args(**inputs), [])


def test_actual_cli_rejects_partial_consumer_bundle_before_claiming_coverage():
    run = subprocess.run([sys.executable, str(ROOT / 'scripts/plan-package-factory.py'),
                          '--root', str(ROOT), '--consumer-revision', REVISION],
                         cwd=ROOT, capture_output=True, text=True)
    assert run.returncode != 0
    assert 'consumer contracts, root, immutable revision and required coverage must be supplied together' in run.stderr
    assert not run.stdout.strip()
