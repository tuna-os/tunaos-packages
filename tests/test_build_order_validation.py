"""Validation must cover all authored manifests and fail closed."""
import importlib.util
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('strict_build_order_parser', ROOT / 'scripts/parse-build-order.py')
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


@pytest.mark.parametrize('path', sorted(ROOT.glob('build-order*.yml')) + sorted((ROOT / '.copr').glob('build-order*.yml')))
def test_all_authored_manifests_have_real_schema(path):
    parser.validate_manifest(path)


def test_required_schema_cannot_fall_back_to_cwd(tmp_path, monkeypatch):
    path = tmp_path / 'build-order.yml'; path.write_text('target: native\ntiers: []\n')
    (tmp_path / 'build-order-schema.json').write_text('{}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(parser, 'SCHEMA_PATH', tmp_path / 'absent/schema.json')
    with pytest.raises(ValueError, match='schema missing'):
        parser.validate_manifest(path)


@pytest.mark.parametrize('package', [
    {'path': '/absolute'}, {'path': '../escape'}, {'path': 'src/../escape'},
    {'path': 'src/native', 'spec_override': '../evil.spec'},
    {'path': 'src/native', 'spec_override': 'not-a-spec.txt'},
    {'path': 'src/native', 'build_tool': 'true'},
    {'path': 'src/native', 'bootstrap': 1},
    {'path': 'src/native', 'unknown': True},
    {'copr_name': 'native', 'spec_override': 'native.spec'}, {},
])
def test_invalid_package_fields_paths_specs_and_types(tmp_path, package):
    path = tmp_path / 'manifest.yml'
    path.write_text(yaml.safe_dump({'target': 'native-x86_64',
        'tiers': [{'name': 'first', 'packages': [package]}]}))
    with pytest.raises(jsonschema.ValidationError):
        parser.validate_manifest(path)


@pytest.mark.parametrize('document', [
    {'target': 'native', 'tiers': [], 'unknown': True},
    {'target': True, 'tiers': []},
    {'target': 'native', 'tiers': [{'name': 'first', 'packages': [{'copr_name': 'pkg'}], 'extra': 1}]},
])
def test_unknown_envelope_and_tier_fields_block(tmp_path, document):
    path = tmp_path / 'manifest.yml'; path.write_text(yaml.safe_dump(document))
    with pytest.raises(jsonschema.ValidationError):
        parser.validate_manifest(path)


def test_duplicate_yaml_keys_and_tiers_block(tmp_path):
    path = tmp_path / 'manifest.yml'; path.write_text('target: one\ntarget: two\n')
    with pytest.raises(ValueError, match='duplicate'):
        parser.validate_manifest(path)
    tier = {'name': 'first', 'packages': [{'copr_name': 'native'}]}
    path.write_text(yaml.safe_dump({'target': 'native', 'tiers': [tier, tier]}))
    with pytest.raises(ValueError, match='duplicate tier'):
        parser.validate_manifest(path)


def test_lint_pins_dependencies_and_gates_all_build_orders():
    workflow = yaml.safe_load((ROOT / '.github/workflows/lint.yml').read_text())
    steps = workflow['jobs']['build-order-validate']['steps']
    commands = '\n'.join(step.get('run', '') for step in steps)
    assert 'python3 -m pip install -r requirements-ci.txt' in commands
    assert 'build-order*.yml .copr/build-order*.yml' in commands
    assert '--validate' in commands
    assert '|| true' not in commands
    assert 'check-jsonschema' not in commands
    assert '/releases/latest/' not in commands
