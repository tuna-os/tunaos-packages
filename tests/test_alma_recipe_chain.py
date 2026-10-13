"""Authored queue semantics; actual native/signature evidence runs in CI."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('alma_recipe_chain', ROOT / 'scripts/alma-recipe-chain.py')
chain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chain)
SHA = 'a' * 40
ACTION = 'sha256:' + 'b' * 64


@pytest.fixture
def source(tmp_path):
    (tmp_path / 'manifests').mkdir()
    target = {'format': 'rpm', 'probe_images': {
        'x86_64': 'example.org/alma@sha256:' + 'c' * 64,
        'aarch64': 'example.org/alma@sha256:' + 'd' * 64},
        'platforms': {'x86_64': 'linux/amd64/v2', 'aarch64': 'linux/arm64'},
        'cpu_baselines': {'x86_64': 'x86-64-v2', 'aarch64': 'armv8-a'}}
    (tmp_path / 'manifests/package-factory.yaml').write_text(yaml.safe_dump({
        'targets': {'alma10': target, 'alma10-kitten': target},
        'dependency_catalog': {'rust': {'alma10': ['rust >= 1.93', 'cargo']}}}))
    return tmp_path


def recipe(root, name, build=(), runtime=(), provides=()):
    path = root / 'packages' / name / 'package.yaml'
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({'name': name, 'version': '1.0',
        'targets': ['alma10', 'alma10-kitten'], 'provides': list(provides),
        'dependencies': {'build': {'common': list(build)}, 'runtime': {'common': list(runtime)}}}))
    return path


def planned(root, packages, architecture='x86_64'):
    return chain.plan(root, 'alma10', architecture, SHA, ACTION, packages)


def test_runtime_provider_chain_preserves_constraints_and_never_readiness(source):
    recipe(source, 'pop-icon-theme', build=['meson'])
    recipe(source, 'cosmic-icon-theme', runtime=['pop-icon-theme >= 3.5'])
    recipe(source, 'cosmic-bg', build=['rust >= 1.93'], runtime=['cosmic-icon-theme'])
    value = planned(source, ['cosmic-bg'])
    assert value['waves'] == [['pop-icon-theme'], ['cosmic-icon-theme'], ['cosmic-bg']]
    assert value['recipes'][-1]['requirements']['build'] == ['meson']
    assert value['nativeRequirements']['cosmic-bg'] == [
        {'phase': 'build', 'nativeExpression': 'rust >= 1.93', 'status': 'unresolved-native-provider'}]
    assert value['status'] == 'blocked' and value['readiness'] is False


def test_architecture_and_source_bound_queue_digest(source):
    recipe(source, 'native')
    a = planned(source, ['native'])
    b = planned(source, ['native'], 'aarch64')
    assert a['platform'] == 'linux/amd64/v2' and b['cpuBaseline'] == 'armv8-a'
    assert a['queueDigest'] != b['queueDigest']
    assert a == planned(source, ['native'])
    assert chain.plan(source, 'alma10', 'x86_64', 'e' * 40, ACTION, ['native'])['queueDigest'] != a['queueDigest']


def test_missing_recipe_blocks(source):
    with pytest.raises(ValueError, match='missing'):
        planned(source, ['absent'])


def test_ambiguous_provider_blocks(source):
    recipe(source, 'one', provides=['library'])
    recipe(source, 'two', provides=['library'])
    recipe(source, 'app', runtime=['library >= 2'])
    with pytest.raises(ValueError, match='ambiguous'):
        planned(source, ['app'])


def test_cycle_blocks(source):
    recipe(source, 'one', runtime=['two'])
    recipe(source, 'two', runtime=['one'])
    with pytest.raises(ValueError, match='cycle'):
        planned(source, ['one'])


def test_capabilities_keep_exact_native_floor(source):
    path = recipe(source, 'app')
    value = yaml.safe_load(path.read_text())
    value['dependencies']['build']['capabilities'] = ['rust']
    path.write_text(yaml.safe_dump(value))
    result = planned(source, ['app'])
    assert result['recipes'][0]['requirements']['build'] == ['cargo', 'rust >= 1.93']
    value['dependencies']['build']['capabilities'] = ['unknown']
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError, match='unresolved capability'):
        planned(source, ['app'])


def test_recipe_symlink_blocks(source):
    path = recipe(source, 'app')
    copy = source / 'outside.yaml'; copy.write_text(path.read_text())
    path.unlink(); path.symlink_to(copy)
    with pytest.raises(ValueError, match='symlink'):
        planned(source, ['app'])


@pytest.mark.parametrize('content', ['{"cell":1,"cell":2}', '{"cell":NaN}'])
def test_identity_json_rejects_ambiguous_material(tmp_path, content):
    path = tmp_path / 'identity.json'; path.write_text(content)
    with pytest.raises(ValueError):
        chain.strict_json(path)


def test_execute_blocks_missing_authenticated_metadata(source, tmp_path):
    recipe(source, 'app')
    with pytest.raises(ValueError, match='authenticated'):
        chain.execute(planned(source, ['app']), root=source, repo=tmp_path / 'repo',
            state=tmp_path / 'state', meta=tmp_path / 'meta', work=tmp_path / 'work',
            builder=lambda *args: pytest.fail('build must not start'),
            verifier=lambda *args: pytest.fail('verification must not start'))


def test_tampered_queue_blocks_before_builder(source, tmp_path):
    recipe(source, 'app')
    value = planned(source, ['app']); value['waves'] = []
    for name in ['meta', 'state']:
        path = tmp_path / name; path.mkdir(); (path / 'identity.json').write_text(json.dumps({}))
    with pytest.raises(ValueError, match='queue differs'):
        chain.execute(value, root=source, repo=tmp_path / 'repo', state=tmp_path / 'state',
            meta=tmp_path / 'meta', work=tmp_path / 'work', builder=lambda *args: pytest.fail('build'),
            verifier=lambda *args: pytest.fail('verify'))


def test_resume_requires_prepared_inputs_and_fresh_verification(source, tmp_path, monkeypatch):
    recipe(source, 'app')
    value = planned(source, ['app'])
    meta = tmp_path / 'meta'; meta.mkdir()
    state = tmp_path / 'state'; state.mkdir()
    repo = tmp_path / 'repo'; repo.mkdir()
    (repo / 'app.rpm').write_bytes(b'authenticated signed bytes')
    identity = {key: value[key] for key in ('cell', 'sourceRevision', 'actionKey', 'target', 'baseDigest', 'cpuBaseline')}
    identity['platform'] = {'os': 'linux', 'architecture': 'amd64', 'variant': 'v2'}
    (meta / 'identity.json').write_text(json.dumps(identity))
    (state / 'identity.json').write_text('{}')
    events = []
    def prepare(*args):
        events.append('prepare'); return tmp_path / 'prepared'
    def skip(*args):
        events.append('input-and-output-check'); return True
    modules = {'candidate': SimpleNamespace(), 'resume': SimpleNamespace(skip=skip,
        completed=lambda _: [{'name': 'app', 'outputs': [{'path': 'app.rpm'}]}])}
    monkeypatch.setattr(chain.importlib.util, 'spec_from_file_location',
        lambda name, path: SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None),
            module=modules['resume' if 'resume' in name else 'candidate']))
    monkeypatch.setattr(chain.importlib.util, 'module_from_spec', lambda spec: spec.module)
    monkeypatch.setattr(chain.subprocess, 'run', lambda *args, **kwargs: events.append('index-or-record'))
    def verify(*args):
        events.append('fresh-native-verification'); return [ACTION]
    result = chain.execute(value, root=source, repo=repo, meta=meta, state=state,
        work=tmp_path / 'work', prepare=prepare,
        resume_outputs=lambda *args: events.append('restore-exact-output-names'),
        builder=lambda *args: pytest.fail('same input must not rebuild'), verifier=verify)
    assert events[:3] == ['prepare', 'input-and-output-check', 'restore-exact-output-names']
    assert 'fresh-native-verification' in events
    assert result['observations'][0]['reused'] is True
    assert result['chainComplete'] is True and result['readiness'] is False


@pytest.mark.parametrize('budget', [0, True, 16201, -1])
def test_budget_cannot_raise_native_soft_limit(source, tmp_path, budget):
    recipe(source, 'app')
    with pytest.raises(ValueError, match='budget'):
        chain.execute(planned(source, ['app']), root=source, repo=tmp_path / 'repo',
            state=tmp_path / 'state', meta=tmp_path / 'meta', work=tmp_path / 'work',
            builder=None, verifier=None, budget_seconds=budget)


@pytest.mark.parametrize('elapsed', [True, -1, float('nan'), float('inf'), '10'])
def test_prephase_elapsed_rejects_invalid_clocks(source, tmp_path, elapsed):
    recipe(source, 'app')
    with pytest.raises(ValueError, match='elapsed'):
        chain.execute(planned(source, ['app']), root=source, repo=tmp_path / 'repo',
            state=tmp_path / 'state', meta=tmp_path / 'meta', work=tmp_path / 'work',
            builder=None, verifier=None, elapsed_seconds=elapsed)


def layer_shell_provider(source, name='gtk4-layer-shell', targets=None):
    # The concrete RPM output shape from PR840 @4f9cff8c (v1.3.0):
    # runtime .so/typelibs and a devel subpackage with a concrete .pc path.
    path = recipe(source, name)
    value = yaml.safe_load(path.read_text())
    if targets is not None:
        value['targets'] = targets
    value['outputs'] = {'rpm': {
        'files': ['usr/lib64/libgtk4-layer-shell.so.*'],
        'subpackages': [{'name': 'devel', 'files': [
            'usr/include/gtk4-layer-shell/gtk4-layer-shell.h',
            'usr/lib64/pkgconfig/gtk4-layer-shell-0.pc']}],
    }}
    path.write_text(yaml.safe_dump(value))
    return path


@pytest.mark.parametrize('requirement', ['gtk4-layer-shell-devel >= 1.1',
                                       'pkgconfig(gtk4-layer-shell-0) >= 1.1'])
def test_real_layer_shell_outputs_order_provider_without_guessing_version(source, requirement):
    layer_shell_provider(source)
    recipe(source, 'tuna-desktop', build=[requirement])
    value = planned(source, ['tuna-desktop'])
    assert value['waves'] == [['gtk4-layer-shell'], ['tuna-desktop']]
    assert value['dependencies']['tuna-desktop'] == ['gtk4-layer-shell']
    record = next(r for r in value['recipes'] if r['name'] == 'tuna-desktop')
    assert record['requirements']['build'] == [requirement]
    assert value['readiness'] is False and value['status'] == 'blocked'
    # Planning must not inject fake base-package Provides into authored metadata.
    authored = yaml.safe_load((source/'packages/gtk4-layer-shell/package.yaml').read_text())
    assert authored['provides'] == []


@pytest.mark.parametrize('requirement', ['gtk4-layer-shell-devel',
                                       'pkgconfig(gtk4-layer-shell-0) >= 1.1'])
def test_output_provider_missing_from_target_is_refused(source, requirement):
    layer_shell_provider(source, targets=['el10'])
    recipe(source, 'tuna-desktop', build=[requirement])
    with pytest.raises(ValueError, match='provider unavailable'):
        planned(source, ['tuna-desktop'])


@pytest.mark.parametrize('requirement', ['pkgconfig(gtk4-layer-shell-0) >= 1.1', 'same-devel'])
def test_output_provider_ambiguity_is_refused(source, requirement):
    layer_shell_provider(source, 'one')
    path = layer_shell_provider(source, 'two')
    if requirement == 'same-devel':
        for name in ['one', 'two']:
            p=source/'packages'/name/'package.yaml'
            v=yaml.safe_load(p.read_text()); v['provides']=['same-devel']; p.write_text(yaml.safe_dump(v))
    recipe(source, 'tuna-desktop', build=[requirement])
    with pytest.raises(ValueError, match='ambiguous'):
        planned(source, ['tuna-desktop'])


@pytest.mark.parametrize('output', ['usr/lib64/pkgconfig/*.pc',
                                   'usr/lib64/pkgconfig/../gtk4-layer-shell-0.pc',
                                   'usr/lib64/other/gtk4-layer-shell-0.pc',
                                   '/usr/lib64/pkgconfig/gtk4-layer-shell-0.pc',
                                   '%{_libdir}/pkgconfig/gtk4-layer-shell-0.pc'])
def test_nonconcrete_pc_paths_do_not_invent_providers(source, output):
    path=layer_shell_provider(source)
    value=yaml.safe_load(path.read_text())
    value['outputs']['rpm']['subpackages'][0]['files']=[output]
    assert 'pkgconfig(gtk4-layer-shell-0)' not in chain.declared_providers(value)


def test_canary_full_and_fixed_tuna_selection(source):
    recipe(source, 'one')
    recipe(source, 'two')
    assert chain.select_roots(source, 'alma10', 'canary') == ['cosmic-icon-theme']
    assert chain.select_roots(source, 'alma10', 'full') == ['one', 'two']
    assert chain.select_roots(source, 'alma10', 'tuna') == ['tuna-desktop']
    with pytest.raises(ValueError, match='scope'):
        chain.select_roots(source, 'alma10', 'arbitrary')
    with pytest.raises(ValueError, match='identity'):
        chain.select_roots(source, 'el10', 'tuna')
    with pytest.raises(ValueError, match='missing'):
        planned(source, chain.select_roots(source, 'alma10', 'tuna'))


def test_current_arch_only_tuna_recipe_fails_closed():
    value = yaml.safe_load((ROOT/'packages/tuna-desktop/package.yaml').read_text())
    assert value['targets'] == ['arch']
    for target in ['alma10', 'alma10-kitten']:
        roots = chain.select_roots(ROOT, target, 'tuna')
        key = chain.selection_action_key(target, 'x86_64', 'tuna', SHA, roots)
        with pytest.raises(ValueError, match='recipe missing for target'):
            chain.plan(ROOT, target, 'x86_64', SHA, key, roots)


def test_selection_identity_changes_with_roots_source_and_architecture():
    key = chain.selection_action_key('alma10', 'x86_64', 'full', SHA, ['one'])
    assert key != chain.selection_action_key('alma10', 'x86_64', 'full', SHA, ['two'])
    assert key != chain.selection_action_key('alma10', 'aarch64', 'full', SHA, ['one'])
    assert key != chain.selection_action_key('alma10', 'x86_64', 'full', 'e'*40, ['one'])
    assert chain.selection_action_key('alma10', 'x86_64', 'full', SHA, ['two','one']) == chain.selection_action_key('alma10', 'x86_64', 'full', SHA, ['one','two'])


def test_output_aware_pkgconfig_cycle_is_refused(source):
    for name, dependency in [('one', 'two'), ('two', 'one')]:
        path = layer_shell_provider(source, name)
        value = yaml.safe_load(path.read_text())
        value['outputs']['rpm']['subpackages'][0]['files'] = [f'usr/lib64/pkgconfig/{name}.pc']
        value['dependencies']['build']['common'] = [f'pkgconfig({dependency}) >= 1.0']
        path.write_text(yaml.safe_dump(value))
    recipe(source, 'tuna-desktop', build=['pkgconfig(one) >= 1.0'])
    with pytest.raises(ValueError, match='provider cycle'):
        planned(source, ['tuna-desktop'])


@pytest.mark.parametrize('outputs', [[], {'rpm': []}, {'rpm': {'files': 'usr/lib64/pkgconfig/fake.pc'}},
                                     {'rpm': {'subpackages': 'devel'}},
                                     {'rpm': {'subpackages': ['devel']}},
                                     {'rpm': {'subpackages': [{'name':'devel', 'files':'fake.pc'}]}},
                                     {'rpm': {'files': [42]}}])
def test_malformed_rpm_output_shapes_fail_closed(outputs):
    with pytest.raises(ValueError, match='RPM'):
        chain.declared_providers({'name':'test', 'outputs':outputs})


def test_string_provides_cannot_invent_character_providers():
    with pytest.raises(ValueError, match='provider declarations'):
        chain.declared_providers({'name':'test', 'provides':'fake'})
