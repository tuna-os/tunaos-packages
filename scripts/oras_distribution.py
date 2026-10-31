#!/usr/bin/env python3
"""ORAS transport for scoped native repositories in COPY-compatible OCI images.

This transports bytes, never installs packages or replaces native signature and
solver gates. Only fresh destinations are accepted; errors have no R2 fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile

VERSION = '1.3.4'
# Default ceiling for the serialized layer, in decimal bytes. This is a
# conservative POLICY, not a measured registry limit. GitHub documents 10 GB
# per layer, yet ghcr.io/tuna-os/tunaos-packages serves a 19,508,954,174-byte
# layer (tag hummingbird-aarch64, measured 2026-10-25). The default stays below
# the documented figure so an unqualified upload fails before it starts. A
# larger layer needs an explicit, validated override (--max-layer-bytes or
# ORAS_MAX_LAYER_BYTES).
DEFAULT_MAX_LAYER_BYTES = 9_000_000_000
MAX_LAYER_ENV = 'ORAS_MAX_LAYER_BYTES'
POSITIVE_INTEGER = re.compile(r'[1-9][0-9]*')
MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
CONFIG = 'application/vnd.oci.image.config.v1+json'
LAYER = 'application/vnd.oci.image.layer.v1.tar'
ARCHES = {'x86_64': 'amd64', 'aarch64': 'arm64'}
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
REF = re.compile(r'ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}')
LABEL = 'org.tunaos.repository.'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def scope(format, target, architecture):
    if format not in {'rpm', 'deb', 'arch', 'gentoo'} or architecture not in ARCHES:
        raise ValueError('unsupported repository format or architecture')
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,95}', target):
        raise ValueError('invalid target')
    return {'format': format, 'target': target, 'architecture': architecture}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def oras(*args):
    if not re.search(r'^Version:\s+' + re.escape(VERSION) + r'\s*$',
                     run('oras', 'version'), re.M):
        raise ValueError(f'ORAS {VERSION} is required')
    return run('oras', *args)


def blob(layout, data, media):
    identity = digest(data)
    (layout / 'blobs' / 'sha256' / identity.split(':')[1]).write_bytes(data)
    return {'mediaType': media, 'digest': identity, 'size': len(data)}


def read_blob(layout, descriptor, media):
    identity = descriptor.get('digest', '')
    if not DIGEST.fullmatch(identity) or descriptor.get('mediaType') != media:
        raise ValueError('invalid descriptor')
    path = layout / 'blobs' / 'sha256' / identity.split(':')[1]
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('oversized image metadata')
    data = path.read_bytes()
    if len(data) != descriptor.get('size') or digest(data) != identity:
        raise ValueError('blob size/digest mismatch')
    return data


def layer_limit(value=None):
    """Return the layer ceiling; reject anything but a positive integer."""
    if value is None:
        return DEFAULT_MAX_LAYER_BYTES
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and POSITIVE_INTEGER.fullmatch(value):
        return int(value)
    raise ValueError(f'layer size limit must be a positive integer of bytes, got {value!r}')


def prepare(repository, layout, selected, max_layer_bytes=None):
    """Make a single-layer scratch image; native metadata/signatures stay intact."""
    limit = layer_limit(max_layer_bytes)
    repository = repository.resolve()
    if not repository.is_dir() or layout.exists():
        raise ValueError('repository must exist and layout must be new')
    paths = sorted(repository.rglob('*'))
    if not any(p.is_file() for p in paths):
        raise ValueError('empty repository')
    # Normalize safe symlinks (e.g. Arch database aliases) to regular files.
    # Never follow a directory symlink or a link outside this repository.
    for path in paths:
        if path.is_symlink():
            if not path.resolve().is_relative_to(repository) or not path.resolve().is_file():
                raise ValueError('unsafe repository symlink')
        elif not path.is_file() and not path.is_dir():
            raise ValueError('special files are not repository content')
    entries = []
    tar_bytes = 0
    for path in paths:
        if not path.is_file():
            continue
        info = tarfile.TarInfo('repository/' + path.relative_to(repository).as_posix())
        snapshot = path.stat()
        info.size = snapshot.st_size
        info.mode = 0o644
        # Include extended PAX headers, file padding, end markers and record padding.
        tar_bytes += len(info.tobuf(format=tarfile.PAX_FORMAT))
        tar_bytes += ((info.size + 511) // 512) * 512
        serialized = ((tar_bytes + 1024 + 10239) // 10240) * 10240
        if serialized > limit:
            raise ValueError(
                f'repository needs a layer above the {limit}-byte layer size policy '
                f'(default {DEFAULT_MAX_LAYER_BYTES}). The default is conservative policy, '
                'not a measured registry limit: GHCR documents 10 GB per layer but serves a '
                '19,508,954,174-byte layer for hummingbird-aarch64. Qualify the upload, then '
                f'raise the limit with --max-layer-bytes or {MAX_LAYER_ENV}, '
                'or split the transport')
        entries.append((path, info, snapshot))
    if not entries:
        raise ValueError('empty repository after size preflight')
    # Stat timestamps can remain unchanged after a same-size write.
    content_digests = {}
    for path, info, _ in entries:
        with path.open('rb') as source:
            content_digests[info.name] = hashlib.file_digest(source, 'sha256').digest()
    layout.mkdir(parents=True)
    (layout / 'blobs' / 'sha256').mkdir(parents=True)
    # Write one tar copy only after the complete size preflight succeeds.
    layer_path = layout / 'layer.tmp'
    with tarfile.open(layer_path, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for path, info, snapshot in entries:
            current = path.stat()
            if (current.st_ino, current.st_dev, current.st_size, current.st_mtime_ns,
                current.st_ctime_ns) != (snapshot.st_ino, snapshot.st_dev, snapshot.st_size,
                                        snapshot.st_mtime_ns, snapshot.st_ctime_ns):
                raise ValueError('repository changed after size preflight')
            with path.open('rb') as source:
                opened = os.fstat(source.fileno())
                if (opened.st_ino, opened.st_dev, opened.st_size, opened.st_mtime_ns,
                    opened.st_ctime_ns) != (snapshot.st_ino, snapshot.st_dev, snapshot.st_size,
                                           snapshot.st_mtime_ns, snapshot.st_ctime_ns):
                    raise ValueError('repository changed after size preflight')
                archive.addfile(info, source)
                # atime can change on read, so compare only content-relevant fields.
                after = os.fstat(source.fileno())
                if (after.st_size != snapshot.st_size
                        or after.st_mtime_ns != snapshot.st_mtime_ns
                        or after.st_ctime_ns != snapshot.st_ctime_ns):
                    raise ValueError('repository changed during archive creation')
    if layer_path.stat().st_size != serialized or serialized > limit:
        raise ValueError('serialized layer size violates publication preflight')
    # Check the archived bytes, not a second read of a possibly restored source.
    with tarfile.open(layer_path, mode='r:') as archive:
        for member in archive:
            with archive.extractfile(member) as source:
                if hashlib.file_digest(source, 'sha256').digest() != content_digests[member.name]:
                    raise ValueError('repository changed after content preflight')
    with layer_path.open('rb') as source:
        layer_digest = 'sha256:' + hashlib.file_digest(source, 'sha256').hexdigest()
    layer = {'mediaType': LAYER, 'digest': layer_digest, 'size': layer_path.stat().st_size}
    layer_path.rename(layout / 'blobs' / 'sha256' / layer_digest.split(':')[1])
    config = {'architecture': ARCHES[selected['architecture']], 'os': 'linux',
              'config': {'Labels': {LABEL + k: v for k, v in selected.items()}},
              'rootfs': {'type': 'layers', 'diff_ids': [layer_digest]}}
    manifest = {'schemaVersion': 2, 'mediaType': MANIFEST,
                'config': blob(layout, encoded(config), CONFIG), 'layers': [layer]}
    descriptor = blob(layout, encoded(manifest), MANIFEST)
    descriptor['annotations'] = {'org.opencontainers.image.ref.name': 'repository'}
    (layout / 'index.json').write_bytes(encoded({'schemaVersion': 2, 'manifests': [descriptor]}))
    (layout / 'oci-layout').write_bytes(encoded({'imageLayoutVersion': '1.0.0'}))
    return descriptor['digest']


def extract(layout, identity, destination, selected):
    """Verify image identity, scope and every byte before exposing a native repo."""
    if destination.exists() or destination.is_symlink() or not DIGEST.fullmatch(identity):
        raise ValueError('destination must be new and reference immutable')
    descriptors = json.loads((layout / 'index.json').read_text())['manifests']
    matches = [d for d in descriptors if d.get('digest') == identity]
    if len(matches) != 1:
        raise ValueError('requested manifest is absent or ambiguous')
    manifest = json.loads(read_blob(layout, matches[0], MANIFEST))
    if manifest.get('schemaVersion') != 2 or manifest.get('mediaType') != MANIFEST:
        raise ValueError('unsupported manifest schema')
    config = json.loads(read_blob(layout, manifest['config'], CONFIG))
    if (config.get('os') != 'linux'
            or config.get('architecture') != ARCHES[selected['architecture']]):
        raise ValueError('wrong image platform')
    labels = config.get('config', {}).get('Labels', {})
    if any(labels.get(LABEL + k) != v for k, v in selected.items()):
        raise ValueError('wrong repository scope')
    layers = manifest['layers']
    if (len(layers) != 1
            or config.get('rootfs') != {'type': 'layers', 'diff_ids': [layers[0]['digest']]}):
        raise ValueError('expected exactly one uncompressed repository layer')
    layer = layers[0]
    if layer.get('mediaType') != LAYER or not DIGEST.fullmatch(layer.get('digest', '')):
        raise ValueError('unsupported repository layer')
    path = layout / 'blobs' / 'sha256' / layer['digest'].split(':')[1]
    with path.open('rb') as source:
        actual = 'sha256:' + hashlib.file_digest(source, 'sha256').hexdigest()
    if actual != layer['digest'] or path.stat().st_size != layer['size']:
        raise ValueError('layer size/digest mismatch')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        root = pathlib.Path(tmp) / 'repository'
        root.mkdir()
        root.chmod(0o755)
        seen = set()
        expanded = 0
        with tarfile.open(path, 'r:') as archive:
            for member in archive:
                parts = pathlib.PurePosixPath(member.name).parts
                expanded += member.size
                if (member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}
                        or member.sparse is not None
                        or any(k.startswith('GNU.sparse') for k in member.pax_headers)
                        or member.size < 0 or member.size > 64 * 1024**3
                        or expanded > layer['size'] or len(seen) >= 1_000_000
                        or len(parts) < 2 or parts[0] != 'repository'
                        or '..' in parts
                        or pathlib.PurePosixPath(member.name).as_posix() != member.name
                        or member.name in seen
                        or any(p.startswith('.wh.') for p in parts)):
                    raise ValueError('unsafe or duplicate repository member')
                seen.add(member.name)
                target = root.joinpath(*parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                for parent in target.parents:
                    if parent == root.parent:
                        break
                    parent.chmod(0o755)
                with archive.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o644)
        if not seen:
            raise ValueError('empty repository layer')
        os.rename(root, destination)


def pull(reference, destination, selected, identity):
    if not REF.fullmatch(reference) or not identity.startswith('https://github.com/'):
        raise ValueError('digest-pinned GHCR ref and exact GitHub signer identity required')
    # Verify before fetching/extracting; no regexp signer, insecure TLS or fallback.
    run('cosign', 'verify', reference, '--certificate-identity', identity,
        '--certificate-oidc-issuer', 'https://token.actions.githubusercontent.com')
    with tempfile.TemporaryDirectory() as tmp:
        layout = pathlib.Path(tmp) / 'layout'
        oras('cp', '--to-oci-layout', reference, str(layout) + ':repository')
        extract(layout, reference.split('@')[1], destination, selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'pull'])
    parser.add_argument('--format', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--architecture', required=True)
    parser.add_argument('--repository', type=pathlib.Path)
    parser.add_argument('--layout', type=pathlib.Path)
    parser.add_argument('--reference')
    parser.add_argument('--destination', type=pathlib.Path)
    parser.add_argument('--identity')
    parser.add_argument('--max-layer-bytes',
                        help=f'prepare only: layer size ceiling in bytes; default '
                             f'{DEFAULT_MAX_LAYER_BYTES}, or ${MAX_LAYER_ENV} when set')
    args = parser.parse_args()
    selected = scope(args.format, args.target, args.architecture)
    if args.operation == 'prepare':
        if not args.repository or not args.layout:
            parser.error('prepare requires --repository and --layout')
        override = args.max_layer_bytes
        if override is None:
            override = os.environ.get(MAX_LAYER_ENV)
        try:
            layer_limit(override)
        except ValueError as error:
            parser.error(str(error))
        print(prepare(args.repository, args.layout, selected, override))
    else:
        if not args.reference or not args.destination or not args.identity:
            parser.error('pull requires --reference, --destination and --identity')
        if args.max_layer_bytes is not None:
            parser.error('--max-layer-bytes applies to prepare only')
        pull(args.reference, args.destination, selected, args.identity)


if __name__ == '__main__':
    main()
