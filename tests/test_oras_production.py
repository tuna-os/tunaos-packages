"""Opt-in production gates do not silently cut over or allow R2 backsliding."""
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('admission', ROOT / 'scripts/validate-oras-admission.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
SEED = 'sha256:' + 'a' * 64


def test_legacy_default_and_explicit_opt_in():
    a.validate(['cell'], 'r2', '', [], {})
    a.validate(['cell'], 'oras', 'true', ['cell'], {'cell': SEED})


@pytest.mark.parametrize('transport,enabled,approved,seeds', [
    ('r2', 'true', ['cell'], {}), ('oras', '', ['cell'], {'cell': SEED}),
    ('oras', 'true', [], {'cell': SEED}), ('oras', 'true', ['cell'], {}),
    ('oras', 'true', ['cell'], {'cell': 'latest'}),
    ('oras', 'true', ['cell'], {'cell': SEED, 'other': SEED}),
    ('oras', 'true', {}, {'cell': SEED}), ('unknown', '', [], {})])
def test_invalid_admission_fails_closed(transport, enabled, approved, seeds):
    with pytest.raises(ValueError):
        a.validate(['cell'], transport, enabled, approved, seeds)


def test_workflow_separates_legacy_and_opt_in_paths():
    doc = yaml.safe_load((ROOT / '.github/workflows/publish-build-chain-rpms.yml').read_text())
    trigger = doc.get('on', doc.get(True))
    assert trigger['workflow_dispatch']['inputs']['distribution']['default'] == 'r2'
    assert doc['concurrency'] == {'group': 'publish-rpms', 'cancel-in-progress': False}
    assert "inputs.distribution != 'oras'" in doc['jobs']['publish']['if']
    assert "inputs.distribution != 'oras'" in doc['jobs']['verify']['if']
    production = doc['jobs']['publish-oras']
    assert "inputs.distribution == 'oras'" in production['if']
    assert 'secrets.R2' not in str(production) and 'rclone' not in str(production)
    assert production['strategy']['max-parallel'] == 1
    assert 'validate-oras-admission.py' in str(doc['jobs']['plan'])


def test_immutable_verified_publish_precedes_channel_advancement():
    text = (ROOT / 'scripts/publish-oras-rpm.sh').read_text()
    assert text.count('[[ "$(current_digest)" = "$SEED_DIGEST" ]]') == 2
    assert text.index('cosign verify') < text.index('podman cp')
    assert text.index('publish-rpm-wave.sh') < text.index('oras cp --from-oci-layout')
    assert text.index('cosign sign') < text.index('oras_distribution.py pull')
    assert text.index('diff -qr') < text.index('oras tag "$ref" "$CELL"')
    assert text.index('publication-intent.json') < text.index('oras tag "$ref" "$CELL"')
    assert 'rclone' not in text and 'secrets.R2' not in text


# Execution harness: every privileged/network/native tool is a recorded stub.
# Real ORAS transport is independently tested in test_oras_distribution.py.
FAKE_TOOL = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ['HARNESS'])
case = os.environ['CASE']
with (root/'calls').open('a') as f: f.write(json.dumps([name, *args])+'\n')
seed = 'sha256:'+'a'*64
candidate = 'sha256:'+'b'*64
if name == 'oras':
    if args == ['version']: print('Version: 1.3.4')
    elif args[:2] == ['manifest', 'fetch']:
        counter = root/'reads'
        n = int(counter.read_text())+1 if counter.exists() else 1
        counter.write_text(str(n))
        if case == 'post_tag_read' and n == 3: sys.exit(1)
        value = seed if n < 3 else candidate
        if (case == 'stale_initial' and n == 1) or (case == 'stale_final' and n == 2): value = candidate
        print(json.dumps({'digest': value}))
    elif args[0] == 'tag' and case == 'tag_failure': sys.exit(1)
    elif args[0] == 'cp' and case == 'upload_failure': sys.exit(1)
elif name == 'jq':
    print(json.load(sys.stdin)['digest'])
elif name == 'podman':
    if args[0] == 'create': print('container')
    if args[0] == 'cp': shutil.copytree(root/'seed', root/'repo', dirs_exist_ok=True)
elif name == 'cosign':
    if case == 'seed_signature' and args[0] == 'verify': sys.exit(1)
    if case == 'sign_failure' and args[0] == 'sign': sys.exit(1)
elif name == 'rpm':
    if args[0] == '--checksig':
        print('package: digests OK' if case == 'unsigned_seed' else 'package: digests signatures OK')
    elif args[0] == '-qp':
        if args[2] == '%{ARCH}':
            print('aarch64' if case == 'wrong_arch' and args[-1].startswith('staged') else 'x86_64')
        else: print('1.el10')
elif name == 'bash':
    if args[0].endswith('build-chain.sh'): print('.el10')
    elif args[0].endswith('verify-native-rpm-repository.sh'):
        if case == 'unsigned_seed' and args[1] == 'repo': sys.exit(1)
        if case == 'wrong_signing_key' and args[1] != 'repo': sys.exit(1)
    elif args[0].endswith('publish-rpm-wave.sh'):
        if case in ('merge_failure', 'empty_wave'): sys.exit(1)
elif name == 'df': print('Avail\n999999999999')
elif name == 'python3':
    if args[1] == 'prepare':
        pathlib.Path(args[args.index('--layout')+1]).mkdir()
        print(candidate)
    elif args[1] == 'pull':
        if case == 'roundtrip_failure': sys.exit(1)
        dest=pathlib.Path(args[args.index('--destination')+1])
        shutil.copytree(root/'repo', dest)
        if case == 'roundtrip_mismatch': (dest/'package.rpm').write_text('wrong')
'''


@pytest.mark.parametrize('case,expected_tag', [
    ('success', True), ('stale_initial', False), ('seed_signature', False),
    ('unsigned_seed', False), ('wrong_signing_key', False), ('wrong_arch', False), ('empty_wave', False),
    ('merge_failure', False), ('upload_failure', False), ('sign_failure', False),
    ('roundtrip_failure', False), ('roundtrip_mismatch', False),
    ('stale_final', False), ('tag_failure', True), ('post_tag_read', True)])
def test_publisher_runtime_failure_boundaries(tmp_path, case, expected_tag):
    import json
    import os
    import shutil
    import subprocess
    import sys

    bin_dir = tmp_path/'bin'
    bin_dir.mkdir()
    # Absolute shebang avoids recursively resolving our python3 stub.
    for name in ['oras', 'jq', 'podman', 'cosign', 'rpm', 'gpg', 'bash', 'df', 'python3']:
        tool = bin_dir/name
        tool.write_text(FAKE_TOOL.replace('#!/usr/bin/env python3', '#!'+sys.executable, 1))
        tool.chmod(0o755)
    seed = tmp_path/'seed'
    (seed/'repodata').mkdir(parents=True)
    (seed/'repodata/repomd.xml').write_text('metadata')
    (seed/'repodata/repomd.xml.asc').write_text('signature')
    (seed/'package.rpm').write_text('package')
    (tmp_path/'staged').mkdir()
    if case != 'empty_wave':
        (tmp_path/'staged/new.rpm').write_text('new')
    (tmp_path/'scratch').mkdir()
    env = dict(os.environ, PATH=str(bin_dir)+':'+os.environ['PATH'], HARNESS=str(tmp_path), CASE=case,
               CELL='gnome50-el10-x86_64', ARCH='x86_64', BUILD_MANIFEST='build-order.yml',
               SEED_DIGEST=SEED, GITHUB_REPOSITORY='tuna-os/tunaos-packages',
               GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1', ORAS_WORKDIR=str(tmp_path/'scratch'),
               GITHUB_OUTPUT=str(tmp_path/'output'), DOCKER_CONFIG=str(tmp_path/'auth'))
    result = subprocess.run([shutil.which('bash'), str(ROOT/'scripts/publish-oras-rpm.sh')],
                            env=env, cwd=tmp_path, capture_output=True, text=True)
    calls = [json.loads(line) for line in (tmp_path/'calls').read_text().splitlines()]
    assert any(c[:2] == ['oras', 'tag'] for c in calls) is expected_tag, result.stderr
    assert (result.returncode == 0) is (case == 'success'), result.stderr
    assert (tmp_path/'publication-receipt.json').exists() is (case == 'success')
    assert (tmp_path/'publication-intent.json').exists() is expected_tag


@pytest.mark.parametrize('case,success', [('signed', True), ('unsigned', False),
                                        ('foreign_rpm_key', False), ('foreign_metadata_key', False)])
def test_native_verifier_isolates_committed_key(tmp_path, case, success):
    import json
    import os
    import shutil
    import subprocess
    import sys

    bin_dir = tmp_path/'bin'
    bin_dir.mkdir()
    stub = '''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['NATIVE_LOG'], 'a') as f: f.write(json.dumps([name, *args])+'\\n')
case = os.environ['NATIVE_CASE']
if name == 'gpg' and '--verify' in args and case == 'foreign_metadata_key': sys.exit(2)
if name == 'rpm':
    if '--checksig' in args:
        if case == 'foreign_rpm_key': sys.exit(1)
        print('package: digests OK' if case == 'unsigned' else 'package: digests signatures OK')
    elif '-qp' in args: print('x86_64')
'''
    for name in ['gpg', 'rpm']:
        tool = bin_dir/name
        tool.write_text('#!'+sys.executable+'\n'+stub)
        tool.chmod(0o755)
    repo = tmp_path/'repo'
    (repo/'repodata').mkdir(parents=True)
    for path in ['repodata/repomd.xml', 'repodata/repomd.xml.asc', 'package.rpm']:
        (repo/path).write_text('fixture')
    log = tmp_path/'calls'
    env = dict(os.environ, PATH=str(bin_dir)+':'+os.environ['PATH'],
               NATIVE_LOG=str(log), NATIVE_CASE=case)
    result = subprocess.run([shutil.which('bash'), str(ROOT/'scripts/verify-native-rpm-repository.sh'),
                             str(repo), 'x86_64'], env=env, cwd=tmp_path, capture_output=True)
    assert (result.returncode == 0) is success
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    gpg_calls = [c for c in calls if c[0] == 'gpg']
    rpm_trust_calls = [c for c in calls if c[0] == 'rpm' and '-qp' not in c]
    assert all('--homedir' in c for c in gpg_calls)
    assert all('--dbpath' in c for c in rpm_trust_calls)
    assert [c[-1] for c in calls if '--import' in c] == ['public.gpg', 'public.gpg']
    assert len({c[c.index('--homedir')+1] for c in gpg_calls}) == 1
    assert len({c[c.index('--dbpath')+1] for c in rpm_trust_calls}) == 1
