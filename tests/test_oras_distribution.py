"""Immutable ORAS transport; no registry credentials or native package execution."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('distribution', ROOT / 'scripts/oras_distribution.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    for name in ['Packages/pkg.rpm', 'repodata/repomd.xml', 'repodata/repomd.xml.asc',
                 'pool/pkg.deb', 'dists/stable/InRelease', 'pkg.pkg.tar.zst',
                 'tuna.db.tar.gz', 'tuna.db.tar.gz.sig', 'public.gpg']:
        p = repo / name
        p.parent.mkdir(exist_ok=True, parents=True)
        p.write_bytes(name.encode())
    (repo / 'tuna.db').symlink_to('tuna.db.tar.gz')
    return repo


@pytest.mark.parametrize('fmt,target', [('rpm', 'gnome50-el10'), ('rpm', 'opensuse'),
                                      ('deb', 'debian'), ('arch', 'arch'), ('gentoo', 'gentoo')])
@pytest.mark.parametrize('arch', ['x86_64', 'aarch64'])
def test_all_formats_and_architectures_roundtrip(tree, tmp_path, fmt, target, arch):
    selected = d.scope(fmt, target, arch)
    layout = tmp_path / 'layout'
    identity = d.prepare(tree, layout, selected)
    # A second build is byte-identical, including manifest identity.
    assert d.prepare(tree, tmp_path / 'again', selected) == identity
    output = tmp_path / 'output'
    d.extract(layout, identity, output, selected)
    for p in tree.rglob('*'):
        if p.is_file():
            assert (output / p.relative_to(tree)).read_bytes() == p.read_bytes()
    manifest = json.loads(d.read_blob(layout, json.loads((layout / 'index.json').read_text())['manifests'][0], d.MANIFEST))
    assert manifest['mediaType'] == d.MANIFEST
    assert manifest['config']['mediaType'] == d.CONFIG


def test_actual_pinned_oras_copy_local_layout(tree, tmp_path):
    if not shutil.which('oras'):
        pytest.skip('official ORAS binary unavailable; not a live registry test')
    selected = d.scope('rpm', 'opensuse', 'x86_64')
    layout = tmp_path / 'layout'
    identity = d.prepare(tree, layout, selected)
    copied = tmp_path / 'copied'
    d.oras('cp', '--from-oci-layout', '--to-oci-layout',
           str(layout) + ':repository', str(copied) + ':repository')
    d.extract(copied, identity, tmp_path / 'output', selected)
    assert (tmp_path / 'output/public.gpg').read_bytes() == b'public.gpg'


@pytest.mark.parametrize('failure', ['scope', 'arch', 'identity', 'manifest', 'config', 'layer'])
def test_corruption_or_wrong_scope_never_exposes_repository(tree, tmp_path, failure):
    selected = d.scope('rpm', 'el10', 'x86_64')
    layout = tmp_path / 'layout'
    identity = d.prepare(tree, layout, selected)
    descriptor = json.loads((layout / 'index.json').read_text())['manifests'][0]
    manifest = json.loads(d.read_blob(layout, descriptor, d.MANIFEST))
    if failure in {'manifest', 'config', 'layer'}:
        damaged = {'manifest': descriptor, 'config': manifest['config'],
                   'layer': manifest['layers'][0]}[failure]
        (layout / 'blobs/sha256' / damaged['digest'].split(':')[1]).write_bytes(b'tampered')
    elif failure == 'identity':
        identity = 'sha256:' + 'a' * 64
    elif failure == 'arch':
        selected = d.scope('rpm', 'el10', 'aarch64')
    else:
        selected = d.scope('deb', 'ubuntu', 'x86_64')
    output = tmp_path / 'output'
    with pytest.raises(ValueError):
        d.extract(layout, identity, output, selected)
    assert not output.exists()


@pytest.mark.parametrize('target', ['/etc/passwd', '../outside', 'directory'])
def test_unsafe_links_rejected(tree, tmp_path, target):
    (tree / 'directory').mkdir()
    (tree / 'bad').symlink_to(target)
    with pytest.raises(ValueError):
        d.prepare(tree, tmp_path / 'layout', d.scope('rpm', 'el10', 'x86_64'))


def test_failed_signature_never_downloads(tree, tmp_path, monkeypatch):
    calls = []
    def fail(*args):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(d, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        d.pull('ghcr.io/tuna-os/packages@sha256:' + 'a' * 64, tmp_path / 'output',
               d.scope('rpm', 'el10', 'x86_64'), 'https://github.com/tuna-os/tunaos-packages/.github/workflows/publish.yml@refs/heads/main')
    assert len(calls) == 1 and calls[0][:2] == ('cosign', 'verify')
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize('ref', ['ghcr.io/tuna-os/packages:latest', 'http://repo.tunaos.org/',
                                'ghcr.io/tuna-os/packages@sha256:abc'])
def test_no_mutable_reference_or_http_fallback(tmp_path, monkeypatch, ref):
    monkeypatch.setattr(d, 'run', lambda *args: pytest.fail('must reject before I/O'))
    with pytest.raises(ValueError):
        d.pull(ref, tmp_path / 'output', d.scope('rpm', 'el10', 'x86_64'),
               'https://github.com/tuna-os/tunaos-packages/.github/workflows/publish.yml@refs/heads/main')


def test_wrong_oras_version_rejected(monkeypatch):
    monkeypatch.setattr(d, 'run', lambda *args: 'Version: 0.1.0\n')
    with pytest.raises(ValueError, match='1.3.4'):
        d.oras('cp', 'source', 'destination')


@pytest.mark.parametrize('member_name,kind', [('../escape', 'file'),
    ('/repository/absolute', 'file'), ('repository/../../escape', 'file'),
    ('repository//alias', 'file'), ('repository/.wh.package', 'file'),
    ('repository/link', 'symlink'), ('repository/hard', 'hardlink'),
    ('repository/sparse', 'sparse'), ('repository/device', 'device'),
    ('repository/duplicate', 'duplicate')])
def test_signed_but_unsafe_archive_is_rejected(tree, tmp_path, member_name, kind):
    import io
    import tarfile

    selected = d.scope('rpm', 'el10', 'x86_64')
    layout = tmp_path / 'layout'
    d.prepare(tree, layout, selected)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        info = tarfile.TarInfo(member_name)
        info.size = 1
        if kind == 'symlink':
            info.type = tarfile.SYMTYPE
            info.linkname = '/tmp/escape'
            info.size = 0
        elif kind == 'hardlink':
            info.type = tarfile.LNKTYPE
            info.linkname = '/tmp/escape'
            info.size = 0
        elif kind == 'sparse':
            info.type = tarfile.GNUTYPE_SPARSE
        elif kind == 'device':
            info.type = tarfile.CHRTYPE
            info.size = 0
        archive.addfile(info, io.BytesIO(b'x'))
        if kind == 'duplicate':
            archive.addfile(info, io.BytesIO(b'x'))
    layer = d.blob(layout, stream.getvalue(), d.LAYER)
    config = {'architecture': 'amd64', 'os': 'linux',
              'config': {'Labels': {d.LABEL + k: v for k, v in selected.items()}},
              'rootfs': {'type': 'layers', 'diff_ids': [layer['digest']]}}
    manifest = {'schemaVersion': 2, 'mediaType': d.MANIFEST,
                'config': d.blob(layout, d.encoded(config), d.CONFIG), 'layers': [layer]}
    desc = d.blob(layout, d.encoded(manifest), d.MANIFEST)
    (layout / 'index.json').write_bytes(d.encoded({'schemaVersion': 2, 'manifests': [desc]}))
    with pytest.raises(ValueError):
        d.extract(layout, desc['digest'], tmp_path / 'output', selected)
    assert not (tmp_path / 'output').exists()
    assert not (tmp_path / 'escape').exists()


def test_registry_failure_has_no_fallback(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(d, 'run', lambda *args: calls.append(args) or '')
    def unavailable(*args):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(d, 'oras', unavailable)
    with pytest.raises(subprocess.CalledProcessError):
        d.pull('ghcr.io/tuna-os/packages@sha256:' + 'a' * 64, tmp_path / 'output',
               d.scope('rpm', 'el10', 'x86_64'),
               'https://github.com/tuna-os/tunaos-packages/.github/workflows/publish.yml@refs/heads/main')
    assert len(calls) == 2
    assert calls[1][0:2] == ('cp', '--to-oci-layout')
    assert not (tmp_path / 'output').exists()


def test_normalized_modes_under_private_umask(tree, tmp_path):
    import os

    selected = d.scope('rpm', 'el10', 'x86_64')
    layout = tmp_path / 'layout'
    identity = d.prepare(tree, layout, selected)
    old = os.umask(0o077)
    try:
        d.extract(layout, identity, tmp_path / 'output', selected)
    finally:
        os.umask(old)
    output = tmp_path / 'output'
    for p in [output, *output.rglob('*')]:
        assert p.stat().st_mode & 0o777 == (0o755 if p.is_dir() else 0o644)


@pytest.mark.parametrize('field,value', [('schemaVersion', 1), ('mediaType', 'invalid')])
def test_malformed_inner_manifest(tree, tmp_path, field, value):
    selected = d.scope('rpm', 'el10', 'x86_64')
    layout = tmp_path / 'layout'
    d.prepare(tree, layout, selected)
    descriptor = json.loads((layout / 'index.json').read_text())['manifests'][0]
    manifest = json.loads(d.read_blob(layout, descriptor, d.MANIFEST))
    manifest[field] = value
    desc = d.blob(layout, d.encoded(manifest), d.MANIFEST)
    (layout / 'index.json').write_bytes(d.encoded({'schemaVersion': 2, 'manifests': [desc]}))
    with pytest.raises(ValueError, match='schema'):
        d.extract(layout, desc['digest'], tmp_path / 'output', selected)


def test_dangling_destination_symlink_rejected(tree, tmp_path):
    selected = d.scope('rpm', 'el10', 'x86_64')
    layout = tmp_path / 'layout'
    identity = d.prepare(tree, layout, selected)
    output = tmp_path / 'output'
    output.symlink_to(tmp_path / 'missing')
    with pytest.raises(ValueError):
        d.extract(layout, identity, output, selected)
    assert output.is_symlink()


def test_serialized_layer_limit_includes_padding(tree, tmp_path, monkeypatch):
    selected = d.scope('rpm', 'el10', 'x86_64')
    reference = tmp_path / 'reference'
    expected = d.prepare(tree, reference, selected)
    desc = json.loads((reference / 'index.json').read_text())['manifests'][0]
    manifest = json.loads(d.read_blob(reference, desc, d.MANIFEST))
    size = manifest['layers'][0]['size']
    monkeypatch.setattr(d, 'DEFAULT_MAX_LAYER_BYTES', size)
    assert d.prepare(tree, tmp_path / 'exact', selected) == expected
    monkeypatch.setattr(d, 'DEFAULT_MAX_LAYER_BYTES', size - 1)
    monkeypatch.setattr(Path, 'open', lambda *a, **k: pytest.fail('must reject before content I/O'))
    with pytest.raises(ValueError, match='layer size policy'):
        d.prepare(tree, tmp_path / 'rejected', selected)
    assert not (tmp_path / 'rejected').exists()


def test_pax_headers_count_toward_layer_limit(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    # Long UTF-8 names require PAX records; ordinary header-only estimates undercount.
    for n in range(5):
        (repo / (str(n) + 'é' * 80)).write_bytes(b'x')
    monkeypatch.setattr(d, 'DEFAULT_MAX_LAYER_BYTES', 10240)
    with pytest.raises(ValueError, match='layer size policy'):
        d.prepare(repo, tmp_path / 'layout', d.scope('rpm', 'el10', 'x86_64'))
    assert not (tmp_path / 'layout').exists()


def test_oversized_single_file_rejected_without_reading(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'large.rpm').write_bytes(b'x' * 10240)
    monkeypatch.setattr(d, 'DEFAULT_MAX_LAYER_BYTES', 10240)
    with pytest.raises(ValueError, match='layer size policy'):
        d.prepare(repo, tmp_path / 'layout', d.scope('rpm', 'el10', 'x86_64'))
    assert not (tmp_path / 'layout').exists()


@pytest.mark.parametrize('new_content', [b'changed-size', b'y'])
def test_source_mutation_after_preflight_fails_closed(tmp_path, monkeypatch, new_content):
    repo = tmp_path / 'repo'
    repo.mkdir()
    package = repo / 'pkg.rpm'
    package.write_bytes(b'x')
    original = d.tarfile.open
    def mutate(*args, **kwargs):
        package.write_bytes(new_content)
        return original(*args, **kwargs)
    monkeypatch.setattr(d.tarfile, 'open', mutate)
    layout = tmp_path / 'layout'
    with pytest.raises(ValueError, match='repository changed'):
        d.prepare(repo, layout, d.scope('rpm', 'el10', 'x86_64'))
    assert not (layout / 'index.json').exists()


@pytest.mark.parametrize('phase', ['before_archive', 'during_archive'])
def test_source_mutation_with_stable_timestamps_fails_closed(tmp_path, monkeypatch, phase):
    repo = tmp_path / 'repo'
    repo.mkdir()
    package = repo / 'pkg.rpm'
    package.write_bytes(b'x')
    snapshot = package.stat()
    original_stat = Path.stat
    original_fstat = os.fstat

    # Model a filesystem whose timestamps do not advance for this same-size write.
    def stable_stat(path, *args, **kwargs):
        current = original_stat(path, *args, **kwargs)
        return snapshot if path == package else current

    def stable_fstat(fd):
        current = original_fstat(fd)
        if (current.st_ino, current.st_dev) == (snapshot.st_ino, snapshot.st_dev):
            return snapshot
        return current

    monkeypatch.setattr(Path, 'stat', stable_stat)
    monkeypatch.setattr(os, 'fstat', stable_fstat)
    if phase == 'before_archive':
        original_open = d.tarfile.open

        def mutate_open(*args, **kwargs):
            if kwargs.get('mode') == 'w':
                package.write_bytes(b'y')
            return original_open(*args, **kwargs)

        monkeypatch.setattr(d.tarfile, 'open', mutate_open)
    else:
        original_addfile = d.tarfile.TarFile.addfile

        def mutate_addfile(archive, info, source=None):
            package.write_bytes(b'y')
            try:
                return original_addfile(archive, info, source)
            finally:
                # Re-reading the source after archiving must not mask changed tar bytes.
                package.write_bytes(b'x')

        monkeypatch.setattr(d.tarfile.TarFile, 'addfile', mutate_addfile)

    layout = tmp_path / 'layout'
    with pytest.raises(ValueError, match='repository changed after content preflight'):
        d.prepare(repo, layout, d.scope('rpm', 'el10', 'x86_64'))
    assert not (layout / 'index.json').exists()
    assert not (layout / 'oci-layout').exists()


HUMMINGBIRD_LAYER = 19_508_954_174
SELECTED = ('rpm', 'el10', 'x86_64')


def probe_layer_size(repo, tmp_path):
    """Serialized layer size of a repository, measured with an unreachable ceiling."""
    probe = tmp_path / 'probe'
    d.prepare(repo, probe, d.scope(*SELECTED), max_layer_bytes=10**12)
    desc = json.loads((probe / 'index.json').read_text())['manifests'][0]
    return json.loads(d.read_blob(probe, desc, d.MANIFEST))['layers'][0]['size']


def test_default_ceiling_is_a_policy_that_says_so(tmp_path, monkeypatch):
    # Default behaviour: an oversize layer is still rejected before any output exists.
    assert d.DEFAULT_MAX_LAYER_BYTES == 9_000_000_000
    monkeypatch.setattr(d, 'DEFAULT_MAX_LAYER_BYTES', 10240)
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'large.rpm').write_bytes(b'x' * 10240)
    with pytest.raises(ValueError) as error:
        d.prepare(repo, tmp_path / 'layout', d.scope(*SELECTED))
    text = str(error.value)
    # The message must not read like a measured registry limit, and must give the way out.
    assert 'not a measured registry limit' in text
    assert f'{HUMMINGBIRD_LAYER:,}' in text
    assert '--max-layer-bytes' in text and d.MAX_LAYER_ENV in text
    assert not (tmp_path / 'layout').exists()


def test_override_accepts_a_layer_above_the_default(tree, tmp_path, monkeypatch):
    selected = d.scope(*SELECTED)
    size = probe_layer_size(tree, tmp_path)
    expected = d.prepare(tree, tmp_path / 'reference', selected)
    monkeypatch.setattr(d, 'DEFAULT_MAX_LAYER_BYTES', size - 1)
    with pytest.raises(ValueError, match='layer size policy'):
        d.prepare(tree, tmp_path / 'default', selected)
    assert not (tmp_path / 'default').exists()
    # Same bytes and manifest identity as an unconstrained run.
    assert d.prepare(tree, tmp_path / 'int', selected, max_layer_bytes=size) == expected
    assert d.prepare(tree, tmp_path / 'text', selected, max_layer_bytes=str(size)) == expected


def test_override_still_rejects_a_layer_above_itself(tree, tmp_path, monkeypatch):
    selected = d.scope(*SELECTED)
    size = probe_layer_size(tree, tmp_path)
    # A default far above the layer must not widen an explicit, smaller override.
    monkeypatch.setattr(d, 'DEFAULT_MAX_LAYER_BYTES', 10**12)
    monkeypatch.setattr(Path, 'open', lambda *a, **k: pytest.fail('must reject before content I/O'))
    for limit in (size - 1, str(size - 1)):
        with pytest.raises(ValueError, match='layer size policy'):
            d.prepare(tree, tmp_path / 'rejected', selected, max_layer_bytes=limit)
        assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('bad', [0, -1, True, 1.5, '', '0', '-5', '+5', ' 5', '5 ', '1.5', '1e10',
                                 '9_000', '0x10', '01', 'abc', '\u0663'])
def test_invalid_override_is_rejected_before_any_work(tree, tmp_path, bad):
    with pytest.raises(ValueError, match='positive integer'):
        d.prepare(tree, tmp_path / 'layout', d.scope(*SELECTED), max_layer_bytes=bad)
    assert not (tmp_path / 'layout').exists()


def run_cli(tmp_path, *extra, env=None, operation='prepare', repo=None):
    environment = {k: v for k, v in os.environ.items() if k != d.MAX_LAYER_ENV}
    environment.update(env or {})
    command = [sys.executable, str(ROOT / 'scripts/oras_distribution.py'), operation,
               '--format', 'rpm', '--target', 'el10', '--architecture', 'x86_64']
    if repo is not None:
        command += ['--repository', str(repo), '--layout', str(tmp_path / 'layout')]
    return subprocess.run([*command, *extra], capture_output=True, text=True, env=environment)


def test_cli_default_matches_the_library(tree, tmp_path):
    expected = d.prepare(tree, tmp_path / 'reference', d.scope(*SELECTED))
    done = run_cli(tmp_path, repo=tree)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == expected


@pytest.mark.parametrize('bad', ['0', '-1', '1.5', '', 'abc'])
def test_cli_rejects_an_invalid_flag_or_environment(tree, tmp_path, bad):
    for done in (run_cli(tmp_path, '--max-layer-bytes', bad, repo=tree),
                 run_cli(tmp_path, env={d.MAX_LAYER_ENV: bad}, repo=tree)):
        assert done.returncode == 2
        assert 'positive integer' in done.stderr
        assert not (tmp_path / 'layout').exists()


def test_cli_flag_beats_environment_and_both_enforce(tree, tmp_path):
    # A valid flag wins over a garbage environment value.
    done = run_cli(tmp_path, '--max-layer-bytes', str(10**12),
                   env={d.MAX_LAYER_ENV: 'abc'}, repo=tree)
    assert done.returncode == 0, done.stderr
    (tmp_path / 'layout').rename(tmp_path / 'first')
    # A valid environment value is honoured, and a too-small one is rejected.
    assert run_cli(tmp_path, env={d.MAX_LAYER_ENV: str(10**12)}, repo=tree).returncode == 0
    (tmp_path / 'layout').rename(tmp_path / 'second')
    small = run_cli(tmp_path, '--max-layer-bytes', '1', repo=tree)
    assert small.returncode != 0
    assert 'layer size policy' in small.stderr
    assert not (tmp_path / 'layout').exists()
    small_env = run_cli(tmp_path, env={d.MAX_LAYER_ENV: '1'}, repo=tree)
    assert small_env.returncode != 0
    assert 'layer size policy' in small_env.stderr


def test_cli_override_above_the_default(tree, tmp_path, monkeypatch, capsys):
    size = probe_layer_size(tree, tmp_path)
    expected = d.prepare(tree, tmp_path / 'reference', d.scope(*SELECTED))
    monkeypatch.setattr(d, 'DEFAULT_MAX_LAYER_BYTES', size - 1)
    base = ['oras_distribution.py', 'prepare', '--format', 'rpm', '--target', 'el10',
            '--architecture', 'x86_64', '--repository', str(tree)]
    monkeypatch.setattr(sys, 'argv', [*base, '--layout', str(tmp_path / 'default')])
    with pytest.raises(ValueError, match='layer size policy'):
        d.main()
    monkeypatch.setattr(sys, 'argv', [*base, '--layout', str(tmp_path / 'flag'),
                                      '--max-layer-bytes', str(size)])
    d.main()
    assert capsys.readouterr().out.strip() == expected
    monkeypatch.setenv(d.MAX_LAYER_ENV, str(size))
    monkeypatch.setattr(sys, 'argv', [*base, '--layout', str(tmp_path / 'env')])
    d.main()
    assert capsys.readouterr().out.strip() == expected


def test_cli_override_is_for_prepare_only(tmp_path):
    done = run_cli(tmp_path, '--reference', 'ghcr.io/tuna-os/x@sha256:' + '0' * 64,
                   '--destination', str(tmp_path / 'out'), '--identity', 'id',
                   '--max-layer-bytes', '5', operation='pull')
    assert done.returncode == 2
    assert 'applies to prepare only' in done.stderr
