"""Candidate assembly fixtures; no Cargo, network, container or publication."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
import yaml

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import tuna_candidate_inputs as candidate
import tideforge
import test_tuna_desktop_el10_staging as legacy


@pytest.fixture
def admitted_fixture(tmp_path, monkeypatch):
    rp, fp, ip, sp, vp, out = legacy.fixture(tmp_path)
    # Transport-only fixtures omit the native adapter requirement. Separate
    # tests restore actual recipe commands and check native flag requirements.
    recipe = yaml.safe_load(rp.read_text())
    recipe['build']['commands'] = [line for line in recipe['build']['commands'] if not line.startswith('export CFLAGS=')]
    rp.write_text(yaml.safe_dump(recipe))
    inputs = json.loads(ip.read_text())
    config = '[source.crates-io]\nreplace-with="vendored-sources"\n[source.vendored-sources]\ndirectory="vendor"\n[net]\noffline=true\n'
    payloads = {'tuna-desktop-source.tar.gz': sp.read_bytes(), candidate.MEMBER: vp.read_bytes(),
                'vendor-config.toml': config.encode()}
    payloads['receipt.json'] = b'API-bound producer fixture'
    archive = tmp_path / 'artifact.zip'
    with zipfile.ZipFile(archive, 'w') as zip:
        for name, data in payloads.items():
            zip.writestr(name, data)
    receipt = {**inputs, 'candidate_only': True, 'release_qualified': False, 'published': False,
               'producer': {'source_sha': 'c'*40, 'workflow': '.github/workflows/tuna-source-inputs.yml',
                            'run_id': 20, 'run_attempt': 1},
               'admission': {'producer_terminal_success': True, 'producer_event': 'workflow_dispatch',
                             'producer_conclusion': 'success', 'artifact_id': 30,
                             'artifact_sha256': candidate.digest(archive.read_bytes()),
                             'artifact_size': archive.stat().st_size},
               'artifacts': {name: {'sha256': candidate.digest(data), 'size': len(data)}
                             for name, data in payloads.items() if name != 'receipt.json'}}
    monkeypatch.setattr(tideforge, 'TARGETS', fp)
    return rp, fp, inputs['cells'], archive, out, receipt


def test_six_cells_one_recipe_local_sources_and_default_refusal(admitted_fixture):
    rp, fp, cells, archive, output, receipt = admitted_fixture
    before = rp.read_bytes()
    result = candidate.assemble(rp, fp, cells, archive, output, receipt)
    assert rp.read_bytes() == before
    assert len(result['cells']) == 6 and result['candidate_only'] and not result['published']
    recipes = list(output.rglob('package.yaml'))
    assert len(recipes) == 1
    recipe = yaml.safe_load(recipes[0].read_text())
    assert recipe['targets'] == ['alma10', 'alma10-kitten', 'el10']
    assert 'url' not in recipe['sources'][0]
    assert recipe['sources'][0]['candidate_input']['artifact_id'] == 30
    assert recipe['build']['cargo_config'].endswith('offline=true\n')
    assert '--locked --offline' in recipe['build']['commands'][-1]
    assert len({cell['recipe_sha256'] for cell in result['cells']}) == 1
    for cell in result['cells']:
        directory = output / f"{cell['consumer']}-{cell['target']}-{cell['arch']}" / 'rpmbuild'
        assert (directory / 'SOURCES' / candidate.MEMBER).read_bytes() == b'hash-bound vendor fixture'
        spec = next((directory / 'SPECS').glob('*.spec')).read_text()
        assert 'Source1:' in spec and candidate.MEMBER in spec
        assert '/releases/download/' not in spec
    with pytest.raises(SystemExit):
        tideforge.validate(recipe, 'alma10')
    with pytest.raises(SystemExit):
        tideforge.render(recipe, 'alma10')
    with pytest.raises(SystemExit):
        tideforge.render_rpm(recipe, 'alma10')
@pytest.mark.parametrize('change', ['sha', 'attempt', 'readiness', 'member', 'producer'])
def test_wrong_admission_or_local_bytes_refused_before_output(admitted_fixture, change):
    rp, fp, cells, archive, output, receipt = admitted_fixture
    if change == 'sha':
        archive.write_bytes(archive.read_bytes() + b'changed')
    elif change == 'attempt':
        receipt['producer']['run_attempt'] = 0
    elif change == 'readiness':
        receipt['release_qualified'] = True
    elif change == 'member':
        receipt['artifacts'][candidate.MEMBER]['sha256'] = '0'*64
    else:
        receipt['admission']['producer_conclusion'] = 'failure'
    with pytest.raises(ValueError):
        candidate.assemble(rp, fp, cells, archive, output, receipt)
    assert not output.exists()


def test_generic_fetch_refuses_candidate_before_any_network(admitted_fixture, tmp_path):
    *_, receipt = admitted_fixture
    source = {'filename': candidate.MEMBER, 'sha256': receipt['vendor_archive_sha256'],
              'candidate_input': candidate.descriptor(receipt), 'destination': '.', 'strip_components': 0}
    path = tmp_path/'candidate-only.yaml'
    path.write_text(yaml.safe_dump({'source': {'url': 'invalid://must-not-fetch', 'sha256': '0'*64}, 'sources': [source]}))
    for script in ['fetch-tideforge-sources.py', 'verify-tideforge-source.py']:
        args = [sys.executable, str(ROOT/'scripts'/script), str(path)]
        if script.startswith('fetch'):
            args.append(str(tmp_path/'fetch'))
        result = subprocess.run(args, capture_output=True, text=True)
        assert result.returncode != 0 and 'generic' in result.stderr


def test_nonexact_arch_compiler_reset_remains_refused(admitted_fixture):
    rp, fp, cells, archive, output, receipt = admitted_fixture
    recipe = yaml.safe_load(rp.read_text())
    recipe['build']['commands'].insert(0, 'export CFLAGS="${CFLAGS:-} -ffat-lto-objects -O2"')
    rp.write_text(yaml.safe_dump(recipe))
    with pytest.raises(SystemExit):
        candidate.assemble(rp, fp, cells, archive, output, receipt)
    assert not output.exists()


def test_context_identity_mismatch_refused(admitted_fixture):
    *_, receipt = admitted_fixture
    source = {'filename': candidate.MEMBER, 'sha256': receipt['vendor_archive_sha256'],
              'candidate_input': candidate.descriptor(receipt), 'destination': '.', 'strip_components': 0}
    context = {source['sha256']: {**source['candidate_input'], 'producer_attempt': 2}}
    with pytest.raises(SystemExit):
        tideforge.validate_source(source, auxiliary=True, candidate_inputs=context)


def test_clean_external_reviewed_helper_required_before_invocation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'check_output', lambda args, **kwargs: 'd'*40+'\n' if 'rev-parse' in args else '')
    monkeypatch.setattr(subprocess, 'run', lambda args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        candidate.independently_admit(tmp_path, 'c'*40, 10, 20, 30)
    assert not calls


def test_fresh_helper_api_receipt_identity_not_inferred(admitted_fixture, tmp_path, monkeypatch):
    *_, receipt = admitted_fixture
    receipt['source'] = {'run_id': 10}
    monkeypatch.setattr(subprocess, 'check_output', lambda args, **kwargs: 'c'*40+'\n' if 'rev-parse' in args else '')
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        Path(args[args.index('--output')+1]).write_text(json.dumps(receipt))
    monkeypatch.setattr(subprocess, 'run', run)
    candidate.independently_admit(tmp_path, 'c'*40, 10, 20, 30)
    assert calls[0][calls[0].index('--producer-sha')+1] == 'c'*40
    receipt['producer']['source_sha'] = 'd'*40
    with pytest.raises(ValueError):
        candidate.independently_admit(tmp_path, 'c'*40, 10, 20, 30)


@pytest.mark.parametrize('shape', ['extra', 'missing', 'duplicate', 'path'])
def test_member_set_refused_even_with_matching_zip_digest(admitted_fixture, shape):
    rp, fp, cells, archive, output, receipt = admitted_fixture
    with zipfile.ZipFile(archive) as source:
        entries = [(entry.filename, source.read(entry)) for entry in source.infolist()]
    if shape == 'extra':
        entries.append(('extra.txt', b'extra'))
    elif shape == 'missing':
        entries = entries[:-1]
    elif shape == 'duplicate':
        entries.append(entries[0])
    else:
        entries[0] = ('../source.tar.gz', entries[0][1])
    with zipfile.ZipFile(archive, 'w') as target:
        for name, data in entries:
            target.writestr(name, data)
    receipt['admission']['artifact_sha256'] = candidate.digest(archive.read_bytes())
    receipt['admission']['artifact_size'] = archive.stat().st_size
    with pytest.raises(ValueError):
        candidate.assemble(rp, fp, cells, archive, output, receipt)
    assert not output.exists()


def test_symlink_zip_and_other_factory_refused(admitted_fixture, tmp_path):
    rp, fp, cells, archive, output, receipt = admitted_fixture
    linked = tmp_path/'linked.zip'
    linked.symlink_to(archive)
    with pytest.raises(ValueError):
        candidate.assemble(rp, fp, cells, linked, output, receipt)
    another = tmp_path/'other-factory.yaml'
    another.write_bytes(fp.read_bytes())
    with pytest.raises(ValueError):
        candidate.assemble(rp, another, cells, archive, output, receipt)
    assert not output.exists()


def test_dirty_admission_helper_refused_before_invocation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'check_output', lambda args, **kwargs: 'c'*40+'\n' if 'rev-parse' in args else ' M source.py')
    monkeypatch.setattr(subprocess, 'run', lambda args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        candidate.independently_admit(tmp_path, 'c'*40, 10, 20, 30)
    assert not calls


@pytest.mark.parametrize('architecture', ['x86_64', 'aarch64'])
def test_actual_recipe_workaround_normalized_all_cells_and_checked_without_flag_reset(admitted_fixture, architecture):
    import os
    rp, fp, cells, archive, output, receipt = admitted_fixture
    actual = yaml.safe_load((ROOT/'packages/tuna-desktop/package.yaml').read_text())
    fixture_recipe = yaml.safe_load(rp.read_text())
    fixture_recipe['build']['commands'] = actual['build']['commands']
    rp.write_text(yaml.safe_dump(fixture_recipe))
    result = candidate.assemble(rp, fp, cells, archive, output, receipt)
    recipe = yaml.safe_load((output/'recipe/package.yaml').read_text())
    assert recipe['build']['require_fat_lto_objects'] is True
    assert len(result['cells']) == 6
    assert actual['build']['commands'][0] not in recipe['build']['commands']
    for cell in result['cells']:
        if cell['arch'] != architecture:
            continue
        spec = next((output/f"{cell['consumer']}-{cell['target']}-{cell['arch']}"/'rpmbuild/SPECS').glob('*.spec')).read_text()
        build = spec.split('\n%build\n',1)[1].split('\n%install\n',1)[0]
        check = tideforge.fat_lto_requirement(recipe, cell['target'])
        assert build.index('%set_build_flags') < build.index(check) < build.index('cargo build')
        if cell['target'] != 'el10':
            assert build.index('tunaos_alma_compiler_policy %{_target_cpu}') < build.index(check)
        baseline = '-march=x86-64-v2' if architecture == 'x86_64' else '-march=armv8-a -mbranch-protection=standard'
        flags = '-O2 -flto=auto -ffat-lto-objects -fstack-protector-strong ' + baseline
        environment = {'PATH': os.environ['PATH'], 'CFLAGS': flags}
        assert subprocess.run(['bash', '-c', check], env=environment).returncode == 0
        assert environment['CFLAGS'] == flags


@pytest.mark.parametrize('flags', ['', '-O2 -flto=thin', '-ffat-lto-objects -fno-fat-lto-objects',
                                  '-fno-fat-lto-objects -ffat-lto-objects', '-ffat-lto-objects-extra', "'unterminated"])
def test_native_fat_lto_requirement_rejects_absent_disabled_or_malformed(flags):
    import os
    check = tideforge.fat_lto_requirement({'build': {'require_fat_lto_objects': True}}, 'el10')
    result = subprocess.run(['bash','-c',check], env={'PATH':os.environ['PATH'], 'CFLAGS':flags}, capture_output=True)
    assert result.returncode != 0


@pytest.mark.parametrize('value', ['true', 1, None, [], {}])
def test_fat_lto_requirement_strict_boolean(value):
    with pytest.raises(SystemExit):
        tideforge.fat_lto_requirement({'build': {'require_fat_lto_objects': value}}, 'alma10')


def test_fat_lto_requirement_unsupported_adapter_refused():
    with pytest.raises(SystemExit):
        tideforge.fat_lto_requirement({'build': {'require_fat_lto_objects': True}}, 'archlinux')


@pytest.mark.parametrize('architecture,baseline', [('x86_64','x86-64-v2'), ('aarch64','armv8-a')])
def test_fat_lto_requirement_preserves_actual_policy_effective_flags_receipt(tmp_path, architecture, baseline):
    import os
    flags = '-O2 -flto=auto -ffat-lto-objects -fstack-protector-strong -D_FORTIFY_SOURCE=3'
    if architecture == 'aarch64':
        flags += ' -mbranch-protection=standard'
    check = tideforge.fat_lto_requirement({'build': {'require_fat_lto_objects': True}}, 'alma10')
    script = 'set -euo pipefail; source "$1"; tunaos_alma_compiler_policy "$2"\n' + check
    environment = {'PATH':os.environ['PATH'], 'CFLAGS':flags,
                   'TUNAOS_COMPILER_EVIDENCE_DIR':str(tmp_path)}
    result = subprocess.run(['bash','-c',script,'policy',str(ROOT/'scripts/alma-compiler-policy.sh'),architecture],
                            env=environment,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    evidence = json.loads((tmp_path/'effective-flags.json').read_text())
    assert evidence['flags']['CFLAGS'] == flags + ' -march=' + baseline
    assert evidence['cpuBaseline'] == baseline and evidence['readiness'] is False


def test_duplicate_exact_workaround_is_not_silently_normalized(admitted_fixture):
    rp, fp, cells, archive, output, receipt = admitted_fixture
    recipe = yaml.safe_load(rp.read_text())
    exact = 'export CFLAGS="${CFLAGS:-} -ffat-lto-objects"'
    recipe['build']['commands'] = [exact, exact, *recipe['build']['commands']]
    rp.write_text(yaml.safe_dump(recipe))
    with pytest.raises(ValueError):
        candidate.assemble(rp, fp, cells, archive, output, receipt)
    assert not output.exists()
