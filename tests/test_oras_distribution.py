"""Immutable ORAS transport; no registry credentials or native package execution."""
import importlib.util
import json
import shutil
import subprocess
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
