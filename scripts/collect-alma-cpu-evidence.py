#!/usr/bin/env python3
"""Collect CPU evidence inside CI, never infer publication readiness.

Run on the CI host against a mounted, clean installed consumer root. The caller
must authenticate --consumer-base-reference and the provenance of compiler
observations. This collector verifies bytes/signatures and records that remaining
producer trust boundary; it does not authenticate the caller or publish packages.
Only evtest on x86-64-v2 currently has a restricted execution profile. Other
missing-note ELF objects remain blocked. No cpio member paths are extracted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shlex
import subprocess
import tempfile
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ALMA_FINGERPRINT = 'EE6DB7B98F5BF5EDD9DA0DE5DEE5C11CC2A1E572'
ALMA_KEY = 'https://repo.almalinux.org/almalinux/RPM-GPG-KEY-AlmaLinux-10'
MAX_RPM = 128 * 1024 * 1024
MAX_PRIMARY = 64 * 1024 * 1024
MAX_XML = 512 * 1024 * 1024
MAX_PACKAGES = 256
IDENTITY_FIELDS = ('name', 'epoch', 'version', 'release', 'architecture')
QUERY = '%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\n'
NATIVE_DBPATH = '/usr/lib/sysimage/rpm'
CPUID_SOURCE = r'''#include <cpuid.h>
#include <stdio.h>
int main(void) {
 unsigned a,b,c,d; __cpuid(1,a,b,c,d);
 unsigned v1=(1u<<0)|(1u<<8)|(1u<<15)|(1u<<23)|(1u<<24)|(1u<<25)|(1u<<26);
 unsigned v2=(1u<<0)|(1u<<9)|(1u<<13)|(1u<<19)|(1u<<20)|(1u<<23);
 printf("v1=%d v2=%d avx=%d\n",(d&v1)==v1,(c&v2)==v2,!!(c&(1u<<28)));
 if((d&v1)!=v1 || (c&v2)!=v2 || (c&(1u<<28))) return 1;
 __cpuid(0x80000001,a,b,c,d);
 printf("lahf=%d longmode=%d\n",!!(c&1),!!(d&(1u<<29)));
 if(!(c&1) || !(d&(1u<<29))) return 2;
 __cpuid_count(7,0,a,b,c,d); printf("avx2=%d\n",!!(b&(1u<<5)));
 return !!(b&(1u<<5));
}
'''
AVX2_SOURCE = '.global _start\n.text\n_start:\n vpbroadcastd %xmm0, %ymm0\n mov $60,%eax\n xor %edi,%edi\n syscall\n'


class CollectionError(ValueError):
    pass


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def bounded_file(path, limit=MAX_RPM):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise CollectionError('missing-or-oversize-input')
    return path.read_bytes()


def save_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    return digest(path.read_bytes())


def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise CollectionError('duplicate-json-key')
        value[key] = item
    return value


def load_json(path):
    return json.loads(bounded_file(path, 1024 * 1024), object_pairs_hook=pairs)


def run(argv, timeout=60):
    """Bound both process time and output; never invoke a shell."""
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RPM, MAX_RPM))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(argv, stdout=out, stderr=err, timeout=timeout,
                                    preexec_fn=limits, env={**os.environ, 'LC_ALL': 'C'})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CollectionError('command-unavailable-or-timeout') from exc
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(MAX_RPM + 1), err.read(MAX_RPM + 1)
        if max(len(stdout), len(stderr)) > MAX_RPM:
            raise CollectionError('command-output-exceeds-bound')
        return {'argv': list(map(str, argv)), 'exitCode': result.returncode,
                'stdout': stdout.decode('utf-8', 'strict'),
                'stderr': stderr.decode('utf-8', 'strict')}


def checked(argv):
    result = run(argv)
    if result['exitCode'] != 0:
        raise CollectionError('command-failed')
    return result


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        before, after = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if after.scheme != 'https' or after.netloc != before.netloc:
            raise CollectionError('cross-origin-redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, destination, limit):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('repo.almalinux.org', 'kitten.repo.almalinux.org') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CollectionError('unapproved-native-url')
    opener = urllib.request.build_opener(SameOriginRedirect())
    try:
        with opener.open(url, timeout=60) as response:
            data = response.read(limit + 1)
            modified = response.headers.get('Last-Modified')
    except (OSError, urllib.error.URLError) as exc:
        status = getattr(exc, 'code', None)
        raise CollectionError('native-download-unavailable: ' + url + ' (HTTP ' + str(status) + ')') from exc
    if len(data) > limit:
        raise CollectionError('native-download-exceeds-bound')
    destination.write_bytes(data)
    return {'url': url, 'path': str(destination), 'digest': digest(data),
            'bytes': len(data), 'lastModified': modified}


def repository_url(url):
    if not re.fullmatch(r'https://(?:repo\.almalinux\.org/almalinux/10|kitten\.repo\.almalinux\.org/10-kitten)/(?:BaseOS|AppStream|CRB)/(?:x86_64_v2|aarch64)/os/', url):
        raise CollectionError('unapproved-native-repository')
    return url


def relative_url(base, path):
    parts = urllib.parse.urlsplit(path)
    if (parts.scheme or parts.netloc or parts.query or parts.fragment or
            path.startswith('/') or '..' in PurePosixPath(path).parts or
            '%' in path or '\\' in path):
        raise CollectionError('unsafe-native-metadata-path')
    return base + path


def native_identity(text):
    lines = text.splitlines()
    if len(lines) != 1 or len(lines[0].split('\t')) != 5:
        raise CollectionError('ambiguous-native-provider')
    fields = lines[0].split('\t')
    if any(not re.fullmatch(r'[A-Za-z0-9_.+~:-]+', value) for value in fields):
        raise CollectionError('invalid-native-identity')
    return dict(zip(IDENTITY_FIELDS, fields))


def identity_key(value):
    return tuple(value[field] for field in IDENTITY_FIELDS)


class Repository:
    def __init__(self, url, output):
        self.url = repository_url(url)
        self.directory = output
        output.mkdir(parents=True, exist_ok=False)
        self.observation = fetch(url + 'repodata/repomd.xml', output / 'repomd.xml', 1024 * 1024)
        raw = (output / 'repomd.xml').read_bytes()
        if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
            raise CollectionError('unsafe-native-xml')
        ns = {'r': 'http://linux.duke.edu/metadata/repo'}
        try:
            entries = [item for item in ET.fromstring(raw).findall('r:data', ns) if item.get('type') == 'primary']
            if len(entries) != 1:
                raise CollectionError('ambiguous-primary-metadata')
            entry = entries[0]
            checksum = entry.find('r:checksum', ns)
            location = entry.find('r:location', ns).get('href')
            expected = checksum.text
            if checksum.get('type') != 'sha256' or not re.fullmatch('[0-9a-f]{64}', expected or ''):
                raise CollectionError('unsupported-primary-checksum')
            self.primary = fetch(relative_url(url, location), output / 'primary.xml.gz', MAX_PRIMARY)
            if self.primary['digest'] != 'sha256:' + expected:
                raise CollectionError('primary-digest-mismatch')
        except (ET.ParseError, AttributeError, TypeError) as exc:
            raise CollectionError('malformed-repository-metadata') from exc

    def find(self, identity):
        namespace = 'http://linux.duke.edu/metadata/common'
        def tag(name):
            return '{' + namespace + '}' + name
        if hasattr(self, '_index'):
            matched = self._index.get(identity_key(identity), [])
            if len(matched) > 1:
                raise CollectionError('ambiguous-native-package-metadata')
            return matched[0] if matched else None
        self._index = {}
        # Decompress once to a bounded private scratch file, never archive paths.
        xml = self.directory / 'primary.xml'
        if not xml.exists():
            total, tail = 0, b''
            with gzip.open(self.directory / 'primary.xml.gz', 'rb') as source, xml.open('wb') as target:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    scan = tail + chunk
                    if total > MAX_XML or b'<!DOCTYPE' in scan or b'<!ENTITY' in scan:
                        raise CollectionError('unsafe-or-oversize-primary')
                    target.write(chunk)
                    tail = chunk[-16:]
        try:
            for _, item in ET.iterparse(xml, events=('end',)):
                if item.tag != tag('package'):
                    continue
                version = item.find(tag('version'))
                actual = {'name': item.findtext(tag('name')), 'epoch': version.get('epoch'),
                          'version': version.get('ver'), 'release': version.get('rel'),
                          'architecture': item.findtext(tag('arch'))}
                checksum = item.find(tag('checksum'))
                if checksum.get('type') != 'sha256' or not re.fullmatch('[0-9a-f]{64}', checksum.text or ''):
                    raise CollectionError('unsupported-package-checksum')
                self._index.setdefault(identity_key(actual), []).append({
                    'url': relative_url(self.url, item.find(tag('location')).get('href')),
                    'digest': 'sha256:' + checksum.text,
                    'metadataDigest': digest(ET.tostring(item))})
                item.clear()
        except (ET.ParseError, AttributeError, TypeError, OSError) as exc:
            raise CollectionError('malformed-primary-metadata') from exc
        matched = self._index.get(identity_key(identity), [])
        if len(matched) > 1:
            raise CollectionError('ambiguous-native-package-metadata')
        return matched[0] if matched else None


def key_fingerprint(key, directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = checked(['gpg', '--homedir', str(directory), '--batch', '--with-colons',
                      '--import-options', 'show-only', '--dry-run', '--import', str(key)])
    lines = result['stdout'].splitlines()
    primary = []
    waiting = False
    for line in lines:
        if line.startswith('pub:'):
            waiting = True
        elif line.startswith('fpr:') and waiting:
            fields = line.split(':')
            if len(fields) < 10 or not re.fullmatch('[A-F0-9]{40}', fields[9]):
                raise CollectionError('malformed-signing-key-fingerprint')
            primary.append(fields[9])
            waiting = False
    if len(primary) != 1:
        raise CollectionError('ambiguous-signing-key')
    return primary[0], result


def verify_signature(rpm, key, fingerprint, directory):
    actual, key_observation = key_fingerprint(key, directory / 'gnupg')
    if actual != fingerprint:
        raise CollectionError('signing-key-fingerprint-mismatch')
    db = directory / 'rpmdb'
    db.mkdir(parents=True, exist_ok=True)
    checked(['rpm', '--dbpath', str(db.resolve()), '--initdb'])
    checked(['rpm', '--dbpath', str(db.resolve()), '--import', str(key)])
    result = checked(['rpm', '--dbpath', str(db.resolve()), '-Kv', str(rpm)])
    text = result['stdout'] + result['stderr']
    if (re.search(r'NOT OK|NOKEY|NOTTRUSTED', text) or
            not re.search(r'Signature.*key ID ' + fingerprint[-8:].lower() + r': OK', text, re.I) or
            'Payload SHA256 digest: OK' not in text):
        raise CollectionError('unverified-rpm-signature')
    return {'fingerprint': actual, 'keyDigest': digest(bounded_file(key)),
            'keyObservation': key_observation, 'verification': result,
            'artifactDigest': digest(bounded_file(rpm))}


def compiler_flags(observation, baseline):
    if (not isinstance(observation, dict) or type(observation.get('schemaVersion')) is not int or observation['schemaVersion'] != 1 or
            observation.get('cpuBaseline') != baseline or not isinstance(observation.get('flags'), dict)):
        raise CollectionError('invalid-compiler-observation')
    flags = []
    for key, value in sorted(observation['flags'].items()):
        if value is not None:
            if not isinstance(value, str):
                raise CollectionError('invalid-compiler-flags')
            flags.extend([key + '=' + value] if key == 'GOAMD64' else shlex.split(value))
    if not flags:
        raise CollectionError('missing-compiler-flags')
    return flags


def native_rpm_command(root, arguments):
    database = (root / NATIVE_DBPATH.lstrip('/')).resolve()
    if not database.is_relative_to(root.resolve()) or not database.is_dir():
        raise CollectionError('missing-or-unsafe-native-rpm-database')
    return ['rpm', '--root', str(root), '--dbpath', NATIVE_DBPATH, *arguments]


def vendor_input_path(output, index):
    # Publication scripts recursively select *.rpm; vendor evidence must never
    # enter that candidate/signing namespace.
    return output / ('native-' + str(index) + '.rpm.input')


def require_installed(root, name, expected):
    actual = native_identity(checked(native_rpm_command(root, ['-q', '--qf', QUERY, name]))['stdout'])
    if actual != expected:
        raise CollectionError('installed-native-identity-mismatch')


def bind_installed_elf(root, name, data):
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise CollectionError('unsafe-installed-elf-path')
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or digest(bounded_file(path)) != digest(data):
        raise CollectionError('installed-elf-byte-mismatch')


def restricted_evtest(cpu, artifacts, payloads, output):
    """Only missing-note loader is covered by this deliberately narrow profile."""
    candidates = [a for a in artifacts if a.get('_nativeIdentity', {}).get('name') == 'evtest']
    if len(candidates) != 1:
        raise CollectionError('unsupported-restricted-execution-profile')
    executable = [b for name, b in payloads[candidates[0]['digest']] if name == './usr/bin/evtest']
    loaders = [(a, b) for a in artifacts for _, b in payloads[a['digest']]
               if cpu.inspect_elf(b).get('soname') == 'ld-linux-x86-64.so.2']
    if len(executable) != 1 or len(loaders) != 1:
        raise CollectionError('ambiguous-restricted-loader-or-executable')
    directory = output / 'restricted'
    directory.mkdir(exist_ok=False)
    # Only fixed, generated paths receive authenticated selected ELF bytes.
    for artifact in artifacts:
        for _, body in payloads[artifact['digest']]:
            soname = cpu.inspect_elf(body).get('soname')
            if soname:
                if not re.fullmatch(r'[A-Za-z0-9_.+-]+', soname):
                    raise CollectionError('unsafe-soname')
                path = directory / soname
                if path.exists() and path.read_bytes() != body:
                    raise CollectionError('ambiguous-execution-library')
                path.write_bytes(body)
                path.chmod(0o755)
    (directory / 'evtest').write_bytes(executable[0])
    (directory / 'evtest').chmod(0o755)
    (directory / 'cpuid.c').write_text(CPUID_SOURCE)
    (directory / 'avx2.S').write_text(AVX2_SOURCE)
    checked(['gcc', '-static', '-march=x86-64', '-o', str(directory / 'cpuid'), str(directory / 'cpuid.c')])
    checked(['gcc', '-nostdlib', '-static', '-o', str(directory / 'avx2'), str(directory / 'avx2.S')])
    runner = Path('/usr/bin/qemu-x86_64-static')
    runner_digest = digest(bounded_file(runner))
    prefix = [str(runner), '-cpu', 'Nehalem-v1']
    positive = checked(prefix + [str(directory / 'cpuid')])
    if not all(token in positive['stdout'] for token in ('v1=1', 'v2=1', 'avx=0', 'lahf=1', 'longmode=1', 'avx2=0')):
        raise CollectionError('restricted-cpuid-not-proved')
    negative = run(prefix + [str(directory / 'avx2')])
    if negative['exitCode'] != -4:
        raise CollectionError('restricted-avx2-control-did-not-sigill')
    loader = directory / 'ld-linux-x86-64.so.2'
    version = checked(prefix + [str(loader), '--version'])
    execution = checked(prefix + [str(loader), '--library-path', str(directory), str(directory / 'evtest'), '--version'])
    if not re.search(r'evtest\s+' + re.escape(candidates[0]['_nativeIdentity']['version']) + r'\b', execution['stdout']):
        raise CollectionError('unexpected-evtest-execution-output')
    evidence = {'runnerDigest': runner_digest, 'runnerVersion': checked([str(runner), '--version']),
                'cpuModel': 'Nehalem-v1', 'positive': positive, 'negative': negative,
                'loaderVersion': version, 'execution': execution,
                'inputs': [{'name': path.name, 'digest': digest(bounded_file(path))} for path in sorted(directory.iterdir())]}
    evidence_digest = save_json(output / 'restricted-execution.json', evidence)
    artifact, body = loaders[0]
    artifact['execution'] = {'artifactDigest': artifact['digest'], 'baseline': 'x86-64-v2',
                             'runnerDigest': runner_digest, 'evidenceDigest': evidence_digest,
                             'exitCode': 0, 'elfDigests': [digest(body)]}


def collect(args, cpu):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = Path(args.consumer_root).resolve()
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}', args.consumer_base_reference) or
            '://' in args.consumer_base_reference or '..' in PurePosixPath(args.consumer_base_reference.split('@')[0]).parts):
        raise CollectionError('consumer-base-not-immutable')
    if not re.fullmatch('[0-9a-f]{40}', args.source_revision) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,160}', args.attempt_identity):
        raise CollectionError('invalid-ci-source-or-attempt-identity')
    observation = {'schemaVersion': 1, 'kind': 'alma-cpu-collection', 'scope': args.scope,
                   'measuredAt': datetime.now(timezone.utc).isoformat(),
                   'sourceRevision': args.source_revision, 'attemptIdentity': args.attempt_identity,
                   'consumerBaseReference': args.consumer_base_reference,
                   'requiredTrust': ['authenticated-ci-producer', 'authenticated-consumer-base'],
                   'readiness': False, 'signatures': [], 'transformations': [], 'blockers': []}
    document = {'schemaVersion': 1, 'baseline': args.baseline, 'artifacts': []}
    payloads = {}
    try:
        if args.baseline != 'x86-64-v2':
            raise CollectionError('unsupported-native-arm-execution-profile')
        installed = checked(native_rpm_command(root, ['-qa', '--qf', QUERY]))
        installed['stdout'] = '\n'.join(sorted(installed['stdout'].splitlines())) + '\n'
        observation['installedInventoryDigest'] = save_json(output / 'installed-inventory.json', installed)
        compiler = load_json(args.compiler_observation)
        flags = compiler_flags(compiler, args.baseline)
        compiler_digest = digest(bounded_file(args.compiler_observation))
        (output / 'compiler-observation.json').write_bytes(bounded_file(args.compiler_observation))
        unsigned = args.unsigned_artifact or []
        if args.scope != 'unsigned' and len(unsigned) != len(args.artifact):
            raise CollectionError('missing-pre-sign-artifact-binding')
        if args.scope != 'unsigned' and (not args.candidate_key or not re.fullmatch('[A-F0-9]{40}', args.candidate_fingerprint or '')):
            raise CollectionError('missing-candidate-signing-identity')
        for index, source in enumerate(args.artifact):
            source = Path(source).resolve()
            original = Path(unsigned[index]).resolve() if unsigned else source
            identity = cpu.rpm_identity(source)
            if identity['name'] != 'evtest':
                raise CollectionError('unsupported-candidate-execution-profile')
            if (identity != cpu.rpm_identity(original) or
                    cpu.command_bytes(['rpm2cpio', str(source)]) != cpu.command_bytes(['rpm2cpio', str(original)])):
                raise CollectionError('signing-transformation-changed-native-payload')
            require_installed(root, identity['name'] + '.' + identity['architecture'], identity)
            bodies = cpu.rpm_elfs(source)
            for name, body in bodies:
                bind_installed_elf(root, name, body)
            artifact_digest = digest(bounded_file(source))
            if args.scope != 'unsigned':
                observation['signatures'].append(verify_signature(source, Path(args.candidate_key), args.candidate_fingerprint, output / ('candidate-signature-' + str(index))))
            transformation = {'unsignedArtifactDigest': digest(bounded_file(original)),
                              'signedArtifactDigest': artifact_digest, 'nativeIdentity': identity,
                              'compilerObservationDigest': compiler_digest, 'nativePayloadUnchanged': True}
            observation['transformations'].append(transformation)
            evidence_digest = save_json(output / ('compiler-binding-' + str(index) + '.json'), transformation)
            record = {'path': str(source), 'digest': artifact_digest, '_nativeIdentity': identity,
                      'compiler': {'artifactDigest': artifact_digest, 'flags': flags, 'evidenceDigest': evidence_digest},
                      'dependencyDigests': [], 'libraryProviders': {}}
            document['artifacts'].append(record)
            payloads[artifact_digest] = bodies
        key = output / 'alma10.asc'
        observation['nativeKey'] = fetch(ALMA_KEY, key, 100000)
        native_arch = 'x86_64_v2' if args.baseline == 'x86-64-v2' else 'aarch64'
        if any('/' + native_arch + '/os/' not in repository_url(url) for url in args.repository):
            raise CollectionError('repository-architecture-baseline-mismatch')
        repositories = [Repository(url, output / ('repository-' + str(i))) for i, url in enumerate(args.repository)]
        observation['repositories'] = [{'repomd': repo.observation, 'primary': repo.primary} for repo in repositories]
        by_identity = {identity_key(a['_nativeIdentity']): a for a in document['artifacts']}
        cursor = 0
        while cursor < len(document['artifacts']):
            artifact = document['artifacts'][cursor]
            cursor += 1
            for _, body in payloads[artifact['digest']]:
                for soname in cpu.inspect_elf(body)['needed']:
                    if not re.fullmatch(r'[A-Za-z0-9_.+-]+', soname):
                        raise CollectionError('unsupported-native-soname')
                    query = soname + ('()(64bit)' if args.baseline in ('x86-64-v2', 'armv8-a') else '')
                    identity = native_identity(checked(native_rpm_command(root, ['-q', '--whatprovides', query, '--qf', QUERY]))['stdout'])
                    record = by_identity.get(identity_key(identity))
                    if record is None:
                        if len(by_identity) >= MAX_PACKAGES:
                            raise CollectionError('native-closure-exceeds-bound')
                        matches = [(repo, match) for repo in repositories if (match := repo.find(identity))]
                        if len(matches) != 1:
                            raise CollectionError('missing-or-ambiguous-native-metadata-provider')
                        repo, match = matches[0]
                        native = vendor_input_path(output, len(by_identity))
                        download = fetch(match['url'], native, MAX_RPM)
                        if download['digest'] != match['digest'] or cpu.rpm_identity(native) != identity:
                            raise CollectionError('native-rpm-identity-or-digest-mismatch')
                        signature = verify_signature(native, key, ALMA_FINGERPRINT, output / ('native-signature-' + str(len(by_identity))))
                        signature_digest = save_json(output / ('native-signature-' + str(len(by_identity)) + '.json'), signature)
                        bodies = cpu.rpm_elfs(native)
                        for name, value in bodies:
                            bind_installed_elf(root, name, value)
                        elf_evidence = [{'path': name, **cpu.inspect_elf(value)} for name, value in bodies]
                        baseline_digest = save_json(output / ('native-elf-' + str(len(by_identity)) + '.json'), elf_evidence)
                        record = {'path': str(native), 'digest': download['digest'], '_nativeIdentity': identity,
                                  'dependencyDigests': [], 'libraryProviders': {},
                                  'vendorProof': {'artifactDigest': download['digest'], 'nativeIdentity': identity,
                                                  'repositorySnapshotDigest': repo.observation['digest'],
                                                  'signingIdentity': ALMA_FINGERPRINT,
                                                  'signatureEvidenceDigest': signature_digest,
                                                  'baselineEvidenceDigest': baseline_digest, 'baseline': args.baseline}}
                        document['artifacts'].append(record)
                        by_identity[identity_key(identity)] = record
                        payloads[record['digest']] = bodies
                        observation['signatures'].append(signature)
                    if not any(cpu.inspect_elf(value).get('soname') == soname for _, value in payloads[record['digest']]):
                        raise CollectionError('installed-provider-does-not-supply-soname')
                    artifact['libraryProviders'][soname] = record['digest']
                    if record['digest'] not in artifact['dependencyDigests']:
                        artifact['dependencyDigests'].append(record['digest'])
        missing = any(cpu.inspect_elf(body)['isaNeeded'] is None for artifact in document['artifacts'] for _, body in payloads[artifact['digest']])
        final_inventory = checked(native_rpm_command(root, ['-qa', '--qf', QUERY]))['stdout']
        if '\n'.join(sorted(final_inventory.splitlines())) + '\n' != installed['stdout']:
            raise CollectionError('consumer-inventory-changed-during-collection')
        if missing:
            if args.baseline != 'x86-64-v2':
                observation['blockers'].append('unsupported-native-arm-execution-profile')
            else:
                try:
                    restricted_evtest(cpu, document['artifacts'], payloads, output)
                except CollectionError as exc:
                    observation['blockers'].append(str(exc))
    except (CollectionError, cpu.BaselineError, OSError, ValueError) as exc:
        observation['blockers'].append(str(exc))
    for artifact in document['artifacts']:
        artifact.pop('_nativeIdentity', None)
    save_json(output / 'verifier-input.json', document)
    try:
        result = cpu.verify(document)
    except cpu.BaselineError as exc:
        result = {'status': 'blocked', 'readiness': False, 'blockers': [{'code': str(exc)}]}
    save_json(output / 'verifier-result.json', result)
    observation['status'] = 'blocked' if observation['blockers'] or result['status'] != 'evidence-bound' else 'evidence-bound'
    observation['verifierInputDigest'] = digest((output / 'verifier-input.json').read_bytes())
    observation['verifierResultDigest'] = digest((output / 'verifier-result.json').read_bytes())
    save_json(output / 'collection.json', observation)
    return observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', action='append', required=True)
    parser.add_argument('--unsigned-artifact', action='append')
    parser.add_argument('--compiler-observation', required=True)
    parser.add_argument('--consumer-root', required=True)
    parser.add_argument('--consumer-base-reference', required=True)
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--attempt-identity', required=True)
    parser.add_argument('--baseline', choices=['x86-64-v2', 'armv8-a'], required=True)
    parser.add_argument('--repository', action='append', required=True)
    parser.add_argument('--candidate-key')
    parser.add_argument('--candidate-fingerprint')
    parser.add_argument('--scope', choices=['unsigned', 'candidate', 'public'], required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('alma_cpu_verifier', Path(__file__).with_name('verify-cpu-baseline.py'))
    cpu = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cpu)
    try:
        result = collect(args, cpu)
    except (CollectionError, OSError, ValueError) as exc:
        parser.exit(2, 'CPU collection blocked: ' + str(exc) + '\n')
    print(json.dumps({key: result.get(key) for key in ('status', 'readiness', 'blockers')}, sort_keys=True))
    if result['status'] != 'evidence-bound':
        verifier_result = json.loads((Path(args.output) / 'verifier-result.json').read_text())
        print(json.dumps({'verifierBlockers': verifier_result.get('blockers', [])}, sort_keys=True))
    raise SystemExit(0 if result['status'] == 'evidence-bound' else 1)


if __name__ == '__main__':
    main()
