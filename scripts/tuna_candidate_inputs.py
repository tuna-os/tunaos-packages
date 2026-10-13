"""Assemble reviewed candidate members locally; never fetch generic URLs or publish."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

sys.dont_write_bytecode = True
import yaml
import tideforge

REPO = 'tuna-os/tuna-desktop'
LIMIT = 512 * 1024 * 1024
MEMBER = 'tuna-desktop-vendor.tar.gz'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def descriptor(receipt):
    owner, admission = receipt['producer'], receipt['admission']
    require(receipt['candidate_only'] is True and receipt['release_qualified'] is False
            and receipt['published'] is False and admission['producer_terminal_success'] is True
            and admission['producer_event'] == 'workflow_dispatch'
            and admission['producer_conclusion'] == 'success', 'input is not admitted candidate material')
    require(re.fullmatch(r'[0-9a-f]{40}', owner['source_sha']) and owner['workflow'] == '.github/workflows/tuna-source-inputs.yml',
            'wrong producer identity')
    require(all(type(n) is int and n > 0 for n in (owner['run_id'], owner['run_attempt'], admission['artifact_id'])),
            'invalid candidate IDs')
    require(re.fullmatch(r'[0-9a-f]{64}', admission['artifact_sha256']) and 0 < admission['artifact_size'] <= LIMIT,
            'invalid candidate artifact binding')
    return {'kind': 'github-actions-member', 'repository': REPO, 'producer_sha': owner['source_sha'],
            'producer_run': owner['run_id'], 'producer_attempt': owner['run_attempt'],
            'artifact_id': admission['artifact_id'], 'artifact_sha256': admission['artifact_sha256'],
            'artifact_size': admission['artifact_size'], 'member': MEMBER,
            'member_sha256': receipt['vendor_archive_sha256']}


def independently_admit(helper, expected_sha, source_run, producer_run, artifact_id):
    """The producer SHA is a caller's reviewed input, never inferred from a receipt."""
    helper = Path(helper).resolve()
    require(re.fullmatch(r'[0-9a-f]{40}', expected_sha), 'reviewed producer SHA required')
    head = subprocess.check_output(['git', '-C', str(helper), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(helper), 'status', '--porcelain'], text=True)
    require(head == expected_sha and not dirty, 'admission helper must be exact reviewed clean source')
    with tempfile.TemporaryDirectory(prefix='tuna-admission-') as temp:
        path = Path(temp) / 'admission.json'
        subprocess.run([str(helper / 'scripts/tuna-source-inputs'), 'admit', '--source-run', str(source_run),
                        '--producer-run', str(producer_run), '--producer-sha', expected_sha,
                        '--artifact', str(artifact_id), '--output', str(path)], check=True)
        receipt = json.loads(path.read_text())
    require(receipt['producer']['source_sha'] == expected_sha and receipt['source']['run_id'] == source_run
            and receipt['producer']['run_id'] == producer_run and receipt['admission']['artifact_id'] == artifact_id,
            'independent admission identity mismatch')
    descriptor(receipt)
    return receipt


def verified_members(path, receipt):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= LIMIT, 'unsafe candidate ZIP')
    data = path.read_bytes()
    require(digest(data) == receipt['admission']['artifact_sha256']
            and len(data) == receipt['admission']['artifact_size'], 'candidate ZIP changed since admission')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        expected = set(receipt['artifacts']) | {'receipt.json'}
        require(len(entries) == len(expected) and {e.filename for e in entries} == expected
                and all('/' not in e.filename and not e.is_dir() for e in entries), 'invalid candidate ZIP members')
        require(sum(e.file_size for e in entries) <= LIMIT, 'expanded ZIP budget exceeded')
        payloads = {e.filename: archive.read(e) for e in entries}
    for name, expected in receipt['artifacts'].items():
        require(expected == {'sha256': digest(payloads[name]), 'size': len(payloads[name])}, 'candidate member changed')
    require(digest(path.read_bytes()) == digest(data), 'candidate ZIP changed during read')
    return payloads


def rust_requirement(payloads):
    metadata = json.loads(payloads['cargo-metadata.json'])
    require(isinstance(metadata.get('packages'), list) and metadata['packages'], 'Cargo package metadata missing')
    versions = []
    for package in metadata['packages']:
        value = package.get('rust_version')
        if value is None:
            continue
        require(isinstance(value, str) and re.fullmatch(r'1\.[0-9]+(?:\.[0-9]+)?', value), 'invalid Cargo MSRV')
        numbers = tuple(int(number) for number in value.split('.'))
        versions.append(numbers + (0,) * (3-len(numbers)))
    require(versions, 'Cargo metadata has no measured minimum Rust version')
    return '.'.join(str(number) for number in max(versions))


def assemble(recipe, factory, cells, archive_zip, output, admission, *, native=False, selected_cell=None):
    """Called after fresh independent admission; output remains local and candidate only."""
    output = Path(output)
    require(type(native) is bool, 'native assembly mode must be boolean')
    require(not output.exists(), 'fresh assembly output required')
    require(selected_cell is None or selected_cell in {(c['target'], c['arch']) for c in cells}, 'selected assembly cell missing')
    require(Path(factory).resolve() == tideforge.TARGETS.resolve(), 'renderer must use the admitted factory source')
    factory_bytes = Path(factory).read_bytes()
    payloads = verified_members(archive_zip, admission)
    # Metadata validated by the independent helper binds this exact retained config.
    admitted = {**admission, 'vendor_config': payloads['vendor-config.toml'].decode()}
    member = descriptor(admitted)
    spec = importlib.util.spec_from_file_location('candidate_stager', Path(__file__).with_name('stage-tuna-desktop-el10.py'))
    stager = importlib.util.module_from_spec(spec); spec.loader.exec_module(stager)
    with tempfile.TemporaryDirectory(prefix='tuna-candidate-stage-') as temp:
        root = Path(temp)
        source = root / 'source.tar.gz'; vendor = root / MEMBER; inputs = root / 'inputs.json'
        source.write_bytes(payloads['tuna-desktop-source.tar.gz']); vendor.write_bytes(payloads[MEMBER])
        inputs.write_text(json.dumps({**admitted, 'cells': cells}))
        result = stager.stage(recipe, factory, inputs, source, vendor, root / 'staged', admission=admitted, native_alma=native)
        candidate = yaml.safe_load((root / 'staged/recipe/package.yaml').read_text())
        if native:
            minimum = rust_requirement(payloads)
            build_deps = candidate['dependencies']['build']
            build_deps['capabilities'] = [name for name in build_deps.get('capabilities', []) if name != 'rust']
            build_deps['common'] = [*build_deps.get('common', []), 'rust >= '+minimum, 'cargo >= '+minimum]
            result['minimum_rust_version'] = minimum
            recipe_digest = digest(yaml.safe_dump(candidate, sort_keys=False).encode())
            for cell in result['cells']:
                cell['recipe_sha256'] = recipe_digest
                cell['build_requires'] = tideforge.target_dependencies(candidate, cell['target'])
        context = {candidate['sources'][0]['sha256']: member}
        rendered = {}
        for target in candidate['targets']:
            tideforge.validate(candidate, target, candidate_inputs=context)
            rendered[target] = tideforge.render(candidate, target, candidate_inputs=context)
        require(Path(factory).read_bytes() == factory_bytes, 'factory source changed during assembly')
        output.mkdir()
        (output / 'recipe').mkdir()
        (output / 'recipe/package.yaml').write_text(yaml.safe_dump(candidate, sort_keys=False))
        for cell in result['cells']:
            key = f"{cell['consumer']}-{cell['target']}-{cell['arch']}"
            if selected_cell is not None and (cell['target'], cell['arch']) != selected_cell:
                continue
            directory = output / key / 'rpmbuild'
            (directory / 'SPECS').mkdir(parents=True)
            (directory / 'SOURCES').mkdir()
            for name, contents in rendered[cell['target']].items():
                require(Path(name).name == name, 'unsafe rendered filename')
                (directory / 'SPECS' / name).write_text(contents)
            primary = tideforge.source_filename(candidate['source'], 0)
            for filename, blob in ((primary, payloads['tuna-desktop-source.tar.gz']), (MEMBER, payloads[MEMBER])):
                destination = directory / 'SOURCES' / filename
                destination.write_bytes(blob)
                require(digest(destination.read_bytes()) == digest(blob), 'assembled source write mismatch')
        (output / 'candidate-inputs.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('helper-checkout', 'expected-producer-sha', 'recipe', 'factory', 'cells', 'artifact-zip', 'output'):
        parser.add_argument('--' + name, required=True)
    for name in ('source-run', 'producer-run', 'artifact'):
        parser.add_argument('--' + name, type=int, required=True)
    args = parser.parse_args()
    admitted = independently_admit(args.helper_checkout, args.expected_producer_sha, args.source_run,
                                   args.producer_run, args.artifact)
    assemble(args.recipe, args.factory, json.loads(Path(args.cells).read_text()), args.artifact_zip,
             args.output, admitted)


if __name__ == '__main__':
    main()
