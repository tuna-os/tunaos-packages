"""Pinned image demand reaches factory planning without becoming readiness proof."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TUNAOS = Path(os.environ.get('TUNAOS_CONSUMER_ROOT', str(ROOT / '.test-deps/tunaos')))
SPEC = importlib.util.spec_from_file_location('consumer_bridge', ROOT / 'scripts/consumer_contract.py')
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(bridge)
DIGEST = 'sha256:' + 'a' * 64
TARGET = {'variant': 'yellowfin', 'flavor': 'cosmic', 'platform': 'linux/amd64/v2',
          'cpuBaseline': 'x86-64-v2', 'hardwareScope': 'generic'}
KEY = 'yellowfin:cosmic:linux/amd64/v2'


def canonical_digest(document):
    # Fixture identities use an independent serializer, never the shared subject.
    body = json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return 'sha256:' + hashlib.sha256(body).hexdigest()


def contract(revision, expression='cosmic-session', name='cosmic-session', constraint=None):
    demand = {'nativeExpression': expression, 'manager': 'dnf', 'scope': 'final', 'required': True,
              'origins': [{'path': 'manifests/desktops/cosmic.yaml', 'phase': 'desktop'}]}
    if name is not None:
        demand['name'] = name
    if constraint is not None:
        demand['constraint'] = constraint
    base_reference = 'quay.io/example/base@' + DIGEST
    result = {'schemaVersion': 1, 'kind': 'consumer-contract', 'target': copy.deepcopy(TARGET),
        'sourceRevision': revision, 'baseReference': base_reference, 'baseDigest': DIGEST,
        'baseResolution': {'configuredReference': 'quay.io/example/base:stable', 'reference': base_reference,
            'indexDigest': DIGEST, 'childDigest': DIGEST, 'configDigest': DIGEST,
            'platform': 'linux/amd64/v2', 'cpuBaseline': 'x86-64-v2',
            'observedPlatform': {'os': 'linux', 'architecture': 'amd64', 'variant': 'v2'},
            'baselineEvidence': [{'url': 'https://example.org/baseline', 'digest': DIGEST}]},
        'packageManager': 'dnf', 'adapter': 'almalinux-kitten-10', 'packageRequirements': [demand],
        'sourceDeclarations': [], 'approvedSources': [], 'nativeGroups': [], 'nativeExcludes': [],
        'nativeVersionLocks': [], 'hooks': [], 'inputs': [{'path': 'manifests/desktops/cosmic.yaml', 'digest': DIGEST}],
        'requiredChecks': ['native-install', 'cpu-baseline'],
        'sourcePolicy': {'inherited': [], 'forbiddenNew': [], 'policyRevision': '9' * 40, 'baselineDigest': DIGEST},
        'resolution': {'status': 'incomplete', 'unresolved': [{'code': 'native-proof-required',
            'origin': 'manifests/desktops/cosmic.yaml', 'detail': 'Measured native transaction required'}]}}
    result['contractDigest'] = canonical_digest(result)
    return result


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clean_consumer(tmp_path):
    assert TUNAOS.is_dir(), 'Prepare the pinned consumer checkout or set TUNAOS_CONSUMER_ROOT'
    pin = json.loads((ROOT / 'tests/consumer-source.json').read_text())
    assert pin['repository'] == 'tuna-os/tunaOS'
    assert git(TUNAOS, 'rev-parse', 'HEAD') == pin['revision'], 'Consumer test checkout must match the recorded full revision'
    root = tmp_path / 'consumer-checkout'
    root.mkdir()
    # Use real shared code/schemas and coverage, with a real immutable checkout.
    for directory in ['schemas', 'scripts/contracts']:
        shutil.copytree(TUNAOS / directory, root / directory,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    (root / '.github').mkdir()
    shutil.copy2(TUNAOS / '.github/build-config.yml', root / '.github/build-config.yml')
    shutil.copy2(TUNAOS / 'required-targets.json', root / 'required-targets.json')
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Contract fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'Fixture ' + tmp_path.name)
    revision = git(root, 'rev-parse', 'HEAD')
    coverage = json.loads((root / 'required-targets.json').read_text())
    rows = []
    for source in coverage['targets']:
        rows.append({**copy.deepcopy(source), 'status': 'blocked', 'contract': None,
                     'reasons': [{'code': 'missing-base-record', 'detail': 'No exact base record'}]})
    document = {'schemaVersion': 1, 'kind': 'consumer-preparation', 'sourceRevision': revision,
                'coverageDigest': coverage['coverageDigest'], 'targets': rows}
    path = tmp_path / 'preparation.json'
    path.write_text(json.dumps(document))
    return root, revision, path, document


def load_fixture(fixture):
    root, revision, path, _ = fixture
    return bridge.load_contracts(path, root, revision, root / 'required-targets.json')


def add_contract(fixture, candidate=None):
    root, revision, path, document = fixture
    candidate = candidate or contract(revision)
    row = next(row for row in document['targets'] if row['target'] == TARGET)
    row.update(contract=candidate, status=candidate['resolution']['status'], reasons=candidate['resolution']['unresolved'])
    path.write_text(json.dumps(document))
    return row


def planning_inputs(candidate=None):
    candidate = candidate or contract('1' * 40)
    inputs = {'consumerRevision': candidate['sourceRevision'], 'coverageDigest': DIGEST,
              'targets': [{'target': copy.deepcopy(TARGET), 'required': True, 'scheduled': True,
                           'contract': candidate, 'reasons': candidate['resolution']['unresolved']}]}
    factory = {'targets': {'alma': {}}, 'consumer_adapters': {'yellowfin': {'target': 'alma', 'manager': 'dnf',
        'architectures': {'linux/amd64/v2': 'x86_64', 'linux/arm64': 'aarch64'},
        'cpuBaselines': ['x86-64-v2', 'armv8-a']}}}
    cells = [{'id': 'session-cell', 'target': 'alma', 'architecture': 'x86_64',
              'platform': 'linux/amd64/v2', 'cpuBaseline': 'x86-64-v2'}]
    return inputs, factory, cells


def provider(name='cosmic-session', cell='session-cell', **changes):
    result = {'id': name, 'name': name, 'nativeExpression': name, 'target': 'alma', 'architecture': 'x86_64',
              'cpuBaseline': 'x86-64-v2', 'manager': 'dnf', 'cellId': cell,
              'sourceIdentity': {'revision': '2' * 40}, 'buildDependencies': [], 'runtimeDependencies': []}
    result.update(changes)
    return result


def codes(row):
    return {item['code'] for item in row['unresolved']}


def test_clean_pinned_checkout_keeps_all_269_missing_base_rows(clean_consumer):
    loaded = load_fixture(clean_consumer)
    assert len(loaded['targets']) == 269
    assert loaded['consumerRevision'] == clean_consumer[1]
    assert all(row['status'] == 'blocked' and row['contract'] is None for row in loaded['targets'])
    assert all(row['reasons'] == [{'code': 'missing-base-record', 'detail': 'No exact base record'}] for row in loaded['targets'])
    factory = yaml.safe_load((ROOT / 'manifests/package-factory.yaml').read_text())
    report = bridge.plan_consumers(loaded, factory, [])
    assert len(report['consumers']) == 269
    assert all(not row['readiness'] and 'missing-base-record' in codes(row) for row in report['consumers'])
    assert report['waves'] == []
    assert report['readiness'] is False


def test_clean_loader_preserves_native_constraint_hash_base_and_source(clean_consumer):
    candidate = contract(clean_consumer[1], 'cosmic-session >= 1:1.0-2.el10', constraint='>= 1:1.0-2.el10')
    add_contract(clean_consumer, candidate)
    loaded = load_fixture(clean_consumer)
    inputs, factory, cells = planning_inputs(candidate)
    report = bridge.plan_consumers(loaded, factory, cells)
    row = next(row for row in report['consumers'] if row['targetKey'] == KEY)
    assert row['sourceRevision'] == clean_consumer[1]
    assert row['contractDigest'] == candidate['contractDigest']
    assert row['baseDigest'] == DIGEST
    assert row['baseReference'] == 'quay.io/example/base@' + DIGEST
    assert row['demand'] == candidate['packageRequirements']
    assert row['baseResolution'] == candidate['baseResolution']
    assert 'unresolved-provider' in codes(row)
    assert row['readiness'] is False


@pytest.mark.parametrize('mutation', ['drop-row', 'duplicate-row', 'revision', 'coverage-hash', 'missing-reason'])
def test_preparation_envelope_rejects_missing_duplicate_or_conflicting_inputs(clean_consumer, mutation):
    root, revision, path, document = clean_consumer
    if mutation == 'drop-row':
        document['targets'].pop()
    elif mutation == 'duplicate-row':
        document['targets'].append(copy.deepcopy(document['targets'][0]))
    elif mutation == 'revision':
        document['sourceRevision'] = '3' * 40
    elif mutation == 'coverage-hash':
        document['coverageDigest'] = 'sha256:' + 'b' * 64
    else:
        document['targets'][0]['reasons'] = []
    path.write_text(json.dumps(document))
    with pytest.raises(bridge.ConsumerContractError):
        load_fixture(clean_consumer)


@pytest.mark.parametrize('mutation', ['hash', 'revision', 'target'])
def test_contract_identity_is_not_replaced_by_envelope_identity(clean_consumer, mutation):
    candidate = contract(clean_consumer[1])
    if mutation == 'hash':
        candidate['contractDigest'] = 'sha256:' + 'b' * 64
    elif mutation == 'revision':
        candidate = contract('3' * 40)
    else:
        candidate['target']['flavor'] = 'gnome'
        candidate['contractDigest'] = canonical_digest({key: value for key, value in candidate.items() if key != 'contractDigest'})
    add_contract(clean_consumer, candidate)
    with pytest.raises(bridge.ConsumerContractError):
        load_fixture(clean_consumer)


@pytest.mark.parametrize('path', ['schemas/consumer-contract.schema.json', 'scripts/contracts/evidence.py',
                                  '.github/build-config.yml', 'required-targets.json'])
def test_dirty_shared_material_cannot_supply_trust(clean_consumer, path):
    root = clean_consumer[0]
    with (root / path).open('a') as handle:
        handle.write('\n# dirty input\n')
    with pytest.raises(bridge.ConsumerContractError, match='dirty'):
        load_fixture(clean_consumer)


@pytest.mark.parametrize('revision', ['main', '1' * 7, 'A' * 40, '1' * 40])
def test_loader_requires_actual_checkout_full_sha(clean_consumer, revision):
    root, _, path, _ = clean_consumer
    with pytest.raises(bridge.ConsumerContractError):
        bridge.load_contracts(path, root, revision, root / 'required-targets.json')


def test_duplicate_json_keys_are_rejected(tmp_path):
    path = tmp_path / 'duplicates.json'
    path.write_text('{"kind":"consumer-preparation","kind":"other"}')
    with pytest.raises(bridge.ConsumerContractError, match='duplicate JSON key'):
        bridge.read_json(path)


def test_provider_plan_is_build_work_and_never_measured_readiness():
    inputs, factory, cells = planning_inputs()
    report = bridge.plan_consumers(inputs, factory, cells, {'providers': [provider()]})
    row = report['consumers'][0]
    assert [item['id'] for item in row['plannedProviders']] == ['cosmic-session']
    assert row['measuredProviders'] == []
    assert 'native-provider-verification-required' in codes(row)
    assert row['plannedProviders'][0]['readiness'] is False
    assert row['readiness'] is report['readiness'] is False
    assert report['waves'] == [['session-cell']]
    assert report['cellBindings'] == {'session-cell': [KEY]}


@pytest.mark.parametrize('change', [{'architecture': 'aarch64'}, {'cpuBaseline': 'x86-64-v3'},
                                   {'target': 'centos'}, {'manager': 'apt'}])
def test_wrong_arch_baseline_family_or_manager_cannot_supply_demand(change):
    inputs, factory, cells = planning_inputs()
    report = bridge.plan_consumers(inputs, factory, cells, {'providers': [provider(**change)]})
    row = report['consumers'][0]
    assert row['plannedProviders'] == []
    assert 'unresolved-provider' in codes(row)
    assert row['demand'] == inputs['targets'][0]['contract']['packageRequirements']
    assert row['contractDigest'] == inputs['targets'][0]['contract']['contractDigest']
    assert row['baseDigest'] == DIGEST


def test_ambiguous_provider_is_not_selected_by_input_order():
    inputs, factory, cells = planning_inputs()
    catalog = {'providers': [provider(id='one'), provider(id='two')]}
    report = bridge.plan_consumers(inputs, factory, cells, catalog)
    assert report['consumers'][0]['plannedProviders'] == []
    assert 'ambiguous-provider' in codes(report['consumers'][0])
    assert bridge.plan_consumers(inputs, factory, cells, {'providers': list(reversed(catalog['providers']))}) == report


@pytest.mark.parametrize('identity', [None, {'revision': 'main'}, {'digest': 'sha256:abc'}])
def test_provider_source_identity_requires_an_immutable_pin(identity):
    inputs, factory, cells = planning_inputs()
    report = bridge.plan_consumers(inputs, factory, cells, {'providers': [provider(sourceIdentity=identity)]})
    assert report['consumers'][0]['plannedProviders'] == []
    assert 'missing-provider-source-identity' in codes(report['consumers'][0])


def test_rich_native_constraint_is_preserved_and_explicitly_unproved():
    candidate = contract('1' * 40, 'cosmic-session >= 1:1.0-2.el10', constraint='>= 1:1.0-2.el10')
    inputs, factory, cells = planning_inputs(candidate)
    report = bridge.plan_consumers(inputs, factory, cells, {'providers': [provider(version='1:0.9-1.el10')]})
    row = report['consumers'][0]
    assert row['demand'][0]['constraint'] == '>= 1:1.0-2.el10'
    assert row['demand'][0]['nativeExpression'] == 'cosmic-session >= 1:1.0-2.el10'
    assert 'native-constraint-unproved' in codes(row)
    assert row['readiness'] is False


def test_missing_transitive_runtime_provider_remains_a_diagnostic():
    inputs, factory, cells = planning_inputs()
    item = provider(runtimeDependencies=[{'nativeExpression': 'missing-runtime', 'name': 'missing-runtime'}])
    report = bridge.plan_consumers(inputs, factory, cells, {'providers': [item]})
    row = report['consumers'][0]
    assert any(gap['code'] == 'unresolved-provider' and gap['detail'] == 'missing-runtime' for gap in row['unresolved'])
    assert row['sourceRevision'] == '1' * 40 and row['baseDigest'] == DIGEST
    assert row['readiness'] is False


def test_unmeasured_provider_dependency_closure_is_not_assumed_empty():
    inputs, factory, cells = planning_inputs()
    item = provider()
    del item['runtimeDependencies']
    report = bridge.plan_consumers(inputs, factory, cells, {'providers': [item]})
    assert 'unmeasured-provider-dependency-closure' in codes(report['consumers'][0])


def test_acyclic_packages_in_one_native_family_do_not_create_a_cell_self_cycle():
    inputs, factory, cells = planning_inputs()
    a = provider(runtimeDependencies=[{'nativeExpression': 'runtime-b', 'name': 'runtime-b'}])
    b = provider('runtime-b', runtimeDependencies=[{'nativeExpression': 'runtime-c', 'name': 'runtime-c'}])
    c = provider('runtime-c')
    report = bridge.plan_consumers(inputs, factory, cells, {'providers': [c, a, b]})
    assert report['providerCycles'] == report['cycles'] == report['blockedCells'] == []
    assert report['providerWaves'] == [[KEY + '::runtime-c'], [KEY + '::runtime-b'], [KEY + '::cosmic-session']]
    assert report['waves'] == [['session-cell']]
    assert report['dependencyCells'] == {'session-cell': []}


@pytest.mark.parametrize('self_cycle', [False, True])
def test_real_provider_cycle_is_not_hidden_by_native_family_cell_collapse(self_cycle):
    inputs, factory, cells = planning_inputs()
    if self_cycle:
        providers = [provider(runtimeDependencies=[{'nativeExpression': 'cosmic-session', 'name': 'cosmic-session'}])]
        expected = [[KEY + '::cosmic-session']]
    else:
        providers = [provider(runtimeDependencies=[{'nativeExpression': 'runtime-b', 'name': 'runtime-b'}]),
                     provider('runtime-b', runtimeDependencies=[{'nativeExpression': 'cosmic-session', 'name': 'cosmic-session'}])]
        expected = [[KEY + '::cosmic-session', KEY + '::runtime-b']]
    report = bridge.plan_consumers(inputs, factory, cells, {'providers': providers})
    assert report['providerCycles'] == expected
    assert report['blockedCells'] == ['session-cell']
    assert report['waves'] == []
    assert 'dependency-cycle-or-blocked-wave' in codes(report['consumers'][0])


def test_validly_bound_measurement_is_still_unverified_not_ready():
    inputs, factory, cells = planning_inputs()
    candidate = inputs['targets'][0]['contract']
    measurement = {'target': TARGET, 'sourceRevision': '1' * 40, 'contractDigest': candidate['contractDigest'],
                   'baseDigest': DIGEST, 'evidence': [{'url': 'https://example.org/proof', 'digest': DIGEST}]}
    report = bridge.plan_consumers(inputs, factory, cells, {'measurements': [measurement]})
    row = report['consumers'][0]
    assert len(row['measuredProviders']) == 1
    assert row['measuredProviders'][0]['verified'] is False
    assert row['measuredProviders'][0]['readiness'] is False
    assert row['readiness'] is False


@pytest.mark.parametrize('field', ['sourceRevision', 'contractDigest', 'baseDigest'])
def test_measurement_cannot_substitute_another_consumer_identity(field):
    inputs, factory, cells = planning_inputs()
    candidate = inputs['targets'][0]['contract']
    measurement = {'target': TARGET, 'sourceRevision': '1' * 40, 'contractDigest': candidate['contractDigest'],
                   'baseDigest': DIGEST, 'evidence': [{'url': 'https://example.org/proof', 'digest': DIGEST}]}
    measurement[field] = '3' * 40 if field == 'sourceRevision' else 'sha256:' + 'b' * 64
    report = bridge.plan_consumers(inputs, factory, cells, {'measurements': [measurement]})
    assert report['consumers'][0]['measuredProviders'] == []
    assert 'measured-provider-identity-mismatch' in codes(report['consumers'][0])


def test_authored_cosmic_el10_queue_keeps_all_25_roots():
    document = yaml.safe_load((ROOT / 'manifests/target-queues/cosmic.yaml').read_text())
    assert document['queues']['el10']['roots'] == [
        'pop-icon-theme', 'cosmic-icon-theme', 'cosmic-bg', 'cosmic-comp', 'cosmic-idle',
        'cosmic-notifications', 'cosmic-osd', 'cosmic-panel', 'cosmic-randr', 'cosmic-settings-daemon',
        'cosmic-session', 'cosmic-settings', 'cosmic-greeter', 'xdg-desktop-portal-cosmic',
        'cosmic-app-library', 'cosmic-applets', 'cosmic-files', 'cosmic-launcher', 'cosmic-screenshot',
        'cosmic-term', 'cosmic-wallpapers', 'cosmic-workspaces', 'cosmic-initial-setup', 'cosmic-osk', 'pop-launcher']
    inputs, factory, cells = planning_inputs()
    factory['_consumer_queues'] = {'alma': {'cosmic': document['queues']['el10']['roots']}}
    report = bridge.plan_consumers(inputs, factory, cells)
    assert report['consumers'][0]['queueRoots'] == document['queues']['el10']['roots']
    assert any(gap['code'] == 'unresolved-provider' and gap['detail'] == 'pop-launcher'
               for gap in report['consumers'][0]['unresolved'])


def test_all_14_adapters_declare_platform_architecture_and_cpu_separately():
    factory = yaml.safe_load((ROOT / 'manifests/package-factory.yaml').read_text())
    adapters = factory['consumer_adapters']
    assert set(adapters) == {'albacore', 'yellowfin', 'skipjack', 'bonito', 'bonito-rawhide', 'wahoo',
        'hummingbird', 'sailfin', 'guppy', 'gurnard', 'grouper', 'marlin', 'flounder', 'flounder-sid'}
    assert adapters['yellowfin']['architectures']['linux/amd64/v2'] == 'x86_64'
    assert 'x86-64-v2' in adapters['yellowfin']['cpuBaselines']
    assert adapters['skipjack']['architectures']['linux/amd64'] == 'x86_64'
    assert 'x86-64-v3' in adapters['skipjack']['cpuBaselines']
    assert adapters['grouper']['architectures']['linux/arm64'] == 'arm64'


@pytest.mark.parametrize('change', [{'architecture': 'aarch64'}, {'target': 'centos'},
                                   {'cpuBaseline': 'x86-64-v3'}, {'platform': 'linux/amd64'}])
def test_provider_cannot_relabel_an_incompatible_factory_cell(change):
    inputs, factory, cells = planning_inputs()
    cells[0].update(change)
    report = bridge.plan_consumers(inputs, factory, cells, {'providers': [provider()]})
    assert report['consumers'][0]['plannedProviders'] == []
    assert 'provider-cell-target-mismatch' in codes(report['consumers'][0])


def test_x86_rpm_architecture_does_not_imply_supported_consumer_baseline():
    inputs, factory, cells = planning_inputs()
    factory['consumer_adapters']['yellowfin']['cpuBaselines'] = ['x86-64-v3']
    report = bridge.plan_consumers(inputs, factory, cells)
    assert 'unsupported-consumer-platform-baseline' in codes(report['consumers'][0])
    assert report['consumers'][0]['target']['platform'] == 'linux/amd64/v2'
    assert report['consumers'][0]['target']['cpuBaseline'] == 'x86-64-v2'
    assert report['readiness'] is False


def test_provider_catalog_rejects_duplicate_ids_instead_of_overwriting():
    inputs, factory, cells = planning_inputs()
    with pytest.raises(bridge.ConsumerContractError, match='unique'):
        bridge.plan_consumers(inputs, factory, cells, {'providers': [provider(), provider()]})


def test_queue_roots_are_desktop_scoped_and_do_not_leak_between_consumers():
    inputs, factory, cells = planning_inputs()
    factory['_consumer_queues'] = {'alma': {'cosmic': ['cosmic-session'], 'gnome': ['gnome-shell']}}
    report = bridge.plan_consumers(inputs, factory, cells)
    assert report['consumers'][0]['queueRoots'] == ['cosmic-session']
    assert not any(gap.get('detail') == 'gnome-shell' for gap in report['consumers'][0]['unresolved'])


@pytest.mark.parametrize('field', ['required', 'scheduled'])
def test_preparation_coverage_flags_require_json_booleans(clean_consumer, field):
    root, revision, path, document = clean_consumer
    row = document['targets'][0]
    row[field] = int(row[field])
    path.write_text(json.dumps(document))
    with pytest.raises(bridge.ConsumerContractError):
        load_fixture(clean_consumer)


def test_ignored_bytecode_cannot_replace_pinned_shared_validator(clean_consumer):
    root, _, _, _ = clean_consumer
    (root / '.git/info/exclude').write_text('__pycache__/\n*.pyc\n')
    cache = root / 'scripts/contracts/__pycache__/evidence.cpython-312.pyc'
    cache.parent.mkdir()
    cache.write_bytes(b'untracked executable bytecode')
    assert git(root, 'status', '--porcelain') == ''
    with pytest.raises(bridge.ConsumerContractError, match='cached bytecode'):
        load_fixture(clean_consumer)


def test_ci_uses_the_recorded_consumer_revision_and_pinned_tools():
    import re
    pin = json.loads((ROOT / 'tests/consumer-source.json').read_text())
    assert pin['repository'] == 'tuna-os/tunaOS'
    assert re.fullmatch(r'[0-9a-f]{40}', pin['revision'])
    workflow = yaml.safe_load((ROOT / '.github/workflows/lint.yml').read_text())
    steps = workflow['jobs']['pytest']['steps']
    checkout = next(step for step in steps if step.get('with', {}).get('repository') == pin['repository'])
    assert checkout['with']['ref'] == '${{ steps.consumer-source.outputs.revision }}'
    assert checkout['with']['path'] == '.test-deps/tunaos'
    source = next(step for step in steps if step.get('id') == 'consumer-source')
    assert 'tests/consumer-source.json' in source['run']
    assert 're.fullmatch' in source['run']
    install = next(step for step in steps if step.get('name') == 'Install dependencies')
    assert '-r requirements-ci.txt' in install['run']
    assert all('==' in line for line in (ROOT / 'requirements-ci.txt').read_text().splitlines())
    runner = next(step for step in steps if step.get('name') == 'Run pytest')
    assert runner['env']['TUNAOS_CONSUMER_ROOT'] == '${{ github.workspace }}/.test-deps/tunaos'
    assert runner['env']['PYTHONDONTWRITEBYTECODE'] == '1'
