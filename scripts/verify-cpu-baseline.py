#!/usr/bin/env python3
"""Conservative CPU evidence gate; flags and RPM labels never imply readiness.

Input version 1: {schemaVersion, baseline, artifacts:[{path,digest,compiler:
{artifactDigest,flags,evidenceDigest},dependencyDigests:[sha256...],libraryProviders:{soname:dependencyArtifactDigest},execution:
{artifactDigest,baseline,runnerDigest,evidenceDigest,exitCode,elfDigests:[...]}}]}.
Execution is optional only when every ELF has explicit compatible GNU ISA notes.
Vendor artifacts may replace compiler with vendorProof:{artifactDigest,
nativeIdentity:{name,epoch,version,release,architecture},repositorySnapshotDigest,
signingIdentity,signatureEvidenceDigest,baselineEvidenceDigest,baseline}.
Native identity is checked against the actual RPM header; vendor proof still
requires caller authentication of the pinned repository/signing/baseline evidence.
The caller must authenticate execution/compiler evidence and the restricted CPU
runner before invoking this structural gate; hashes alone do not authenticate it.
Output contains artifact/ELF hashes and explicit blockers. RPM extraction parses
bounded newc bytes itself and never materializes archive paths or symlinks.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import tempfile

LIMIT = 128 * 1024 * 1024
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
BASELINES = {'x86-64-v2': ('Advanced Micro Devices X86-64', 2), 'armv8-a': ('AArch64', 0)}


class BaselineError(ValueError):
    pass


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise BaselineError('duplicate JSON key')
        result[key] = value
    return result


def command_bytes(command):
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (LIMIT, LIMIT))
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(command, stdout=output, stderr=errors, timeout=60, preexec_fn=limits)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BaselineError('inspection command unavailable or timed out') from exc
        if result.returncode != 0:
            raise BaselineError('inspection command failed')
        output.seek(0)
        data = output.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise BaselineError('inspection output exceeds limit')
        return data


def rpm_elfs(path):
    data = command_bytes(['rpm2cpio', str(path)])
    result, position, seen, trailer = [], 0, set(), False
    hardlinks = {}
    single_links = set()
    while position < len(data):
        if position + 110 > len(data) or data[position:position + 6] != b'070701':
            raise BaselineError('unsupported or truncated RPM archive')
        try:
            fields = [int(data[position + 6 + i * 8:position + 14 + i * 8], 16) for i in range(13)]
        except ValueError as exc:
            raise BaselineError('malformed archive header') from exc
        mode, size, namesize = fields[1], fields[6], fields[11]
        position += 110
        if namesize < 1 or namesize > 4096 or position + namesize > len(data):
            raise BaselineError('invalid archive path')
        namebytes = data[position:position + namesize]
        if namebytes[-1:] != b'\0':
            raise BaselineError('unterminated archive path')
        name = os.fsdecode(namebytes[:-1])
        position = (position + namesize + 3) & ~3
        if name == 'TRAILER!!!':
            if size or any(data[position:]):
                raise BaselineError('invalid archive trailer')
            trailer = True
            break
        if name in seen or name.startswith('/') or '..' in Path(name).parts:
            raise BaselineError('unsafe or duplicate archive path')
        seen.add(name)
        if position + size > len(data):
            raise BaselineError('truncated archive payload')
        payload = data[position:position + size]
        position = (position + size + 3) & ~3
        if mode & 0o170000 == 0o100000:
            key = (fields[7], fields[8], fields[0])
            if fields[4] < 1:
                raise BaselineError('invalid regular file link count')
            if fields[4] > 1:
                # newc stores a hardlink group's data on one member. Resolve
                # aliases in memory; never create links or filesystem paths.
                hardlinks.setdefault(key, []).append((name, payload, tuple(fields)))
            else:
                single_links.add(key)
                if payload.startswith(b'\x7fELF'):
                    result.append((name, payload))
    if not trailer:
        raise BaselineError('missing archive trailer')
    for key, members in hardlinks.items():
        if key in single_links:
            raise BaselineError('inconsistent hardlink group identity')
        reference = members[0][2]
        metadata = tuple(reference[i] for i in (1, 2, 3, 4, 5, 9, 10))
        if (len(members) != reference[4] or
                any(tuple(fields[i] for i in (1, 2, 3, 4, 5, 9, 10)) != metadata
                    for _, _, fields in members)):
            raise BaselineError('incomplete or inconsistent hardlink group')
        bodies = [payload for _, payload, _ in members if payload]
        if len(bodies) > 1:
            raise BaselineError('ambiguous hardlink payload')
        payload = bodies[0] if bodies else b''
        if payload.startswith(b'\x7fELF'):
            result.extend((name, payload) for name, _, _ in members)
    return result


def inspect_elf(data):
    with tempfile.NamedTemporaryFile() as stream:
        stream.write(data)
        stream.flush()
        header = command_bytes(['readelf', '-h', stream.name]).decode('utf-8', 'strict')
        notes = command_bytes(['readelf', '-n', stream.name]).decode('utf-8', 'strict')
        dynamic = command_bytes(['readelf', '-d', stream.name]).decode('utf-8', 'strict')
    machine = re.search(r'^\s*Machine:\s*(.+)$', header, re.M)
    if not machine:
        raise BaselineError('missing ELF machine')
    # Only ISA NEEDED is a requirement. ISA USED alone cannot establish a ceiling.
    needed = re.findall(r'x86 ISA needed:\s*([^\n]+)', notes)
    levels = []
    for line in needed:
        tokens = re.findall(r'x86-64-(baseline|v[234])', line)
        if not tokens or '<unknown' in line.lower():
            raise BaselineError('unrecognized ISA needed property')
        levels += [1 if token == 'baseline' else int(token[1:]) for token in tokens]
    return {'digest': digest(data), 'machine': machine.group(1).strip(),
            'isaNeeded': max(levels) if levels else None,
            'needed': re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', dynamic),
            'soname': (re.findall(r'\(SONAME\).*?\[([^\]]+)\]', dynamic) or [None])[0]}


def rpm_identity(path):
    raw = command_bytes(['rpm', '-qp', '--nosignature', '--nodigest', '--qf',
                         '%{NAME}\n%{EPOCHNUM}\n%{VERSION}\n%{RELEASE}\n%{ARCH}\n', str(path)])
    fields = raw.decode('utf-8', 'strict').splitlines()
    if len(fields) != 5 or any(not field for field in fields):
        raise BaselineError('invalid native RPM identity')
    return dict(zip(('name', 'epoch', 'version', 'release', 'architecture'), fields))


def verify(document, inspect=inspect_elf, extract=rpm_elfs, native_identity=rpm_identity):
    if type(document.get('schemaVersion')) is not int or document['schemaVersion'] != 1:
        raise BaselineError('unsupported schema version')
    baseline = document.get('baseline')
    if baseline not in BASELINES:
        raise BaselineError('unsupported baseline')
    artifacts = document.get('artifacts')
    if not isinstance(artifacts, list) or not artifacts:
        raise BaselineError('artifact closure is required')
    by_digest, rows, blockers = {}, [], []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not DIGEST.fullmatch(str(artifact.get('digest', ''))):
            raise BaselineError('invalid artifact identity')
        if artifact['digest'] in by_digest:
            raise BaselineError('duplicate artifact identity')
        by_digest[artifact['digest']] = artifact
    for artifact in artifacts:
        identity = artifact['digest']
        row = {'digest': identity, 'elfs': [], 'blockers': []}
        def gap(code):
            if code not in row['blockers']:
                row['blockers'].append(code)
        path = Path(artifact.get('path', ''))
        if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
            gap('missing-or-oversized-artifact')
        else:
            if digest(path.read_bytes()) != identity:
                gap('artifact-digest-mismatch')
            else:
                try:
                    row['elfs'] = [{'path': name, **inspect(data)} for name, data in extract(path)]
                    row['payloadKind'] = 'elf' if row['elfs'] else 'inspected-no-elf-payload'
                except (BaselineError, UnicodeError) as exc:
                    gap('unsupported-artifact-inspection')
        vendor = artifact.get('vendorProof')
        if vendor is not None:
            valid_vendor = (isinstance(vendor, dict) and vendor.get('artifactDigest') == identity and
                            vendor.get('baseline') == baseline and
                            isinstance(vendor.get('signingIdentity'), str) and bool(vendor['signingIdentity'].strip()) and
                            all(isinstance(vendor.get(field), str) and DIGEST.fullmatch(vendor[field])
                                for field in ('repositorySnapshotDigest', 'signatureEvidenceDigest', 'baselineEvidenceDigest')) and
                            isinstance(vendor.get('nativeIdentity'), dict) and
                            set(vendor['nativeIdentity']) == {'name', 'epoch', 'version', 'release', 'architecture'} and
                            all(isinstance(value, str) and bool(value) for value in vendor['nativeIdentity'].values()))
            if not valid_vendor:
                gap('missing-or-unbound-vendor-proof')
            elif 'artifact-digest-mismatch' not in row['blockers'] and path.is_file():
                try:
                    actual = native_identity(path)
                    if actual != vendor['nativeIdentity']:
                        gap('vendor-native-identity-mismatch')
                    allowed = ('x86_64', 'x86_64_v2', 'noarch') if baseline == 'x86-64-v2' else ('aarch64', 'noarch')
                    if actual.get('architecture') not in allowed:
                        gap('incompatible-vendor-native-architecture')
                except (BaselineError, UnicodeError):
                    gap('unproved-vendor-native-identity')
        else:
            compiler = artifact.get('compiler', {})
            if (not isinstance(compiler, dict) or compiler.get('artifactDigest') != identity or
                    not DIGEST.fullmatch(str(compiler.get('evidenceDigest', ''))) or
                    not isinstance(compiler.get('flags'), list) or not compiler.get('flags') or
                    any(not isinstance(flag, str) for flag in compiler['flags'])):
                gap('missing-bound-compiler-evidence')
            else:
                flags = ' '.join(compiler['flags'])
                expected_cpu = 'x86-64-v2' if baseline == 'x86-64-v2' else 'armv8-a'
                settings = re.findall(r'(-march=|-mcpu=|target-cpu=)([^\s]+)', flags)
                # Rust's AArch64 baseline is named generic. It may accompany
                # independently measured ARMv8 C flags, never replace them.
                proved = any(value == expected_cpu for _, value in settings)
                compatible = all(value == expected_cpu or (
                    baseline == 'armv8-a' and knob == 'target-cpu=' and value == 'generic'
                ) for knob, value in settings)
                if not proved or not compatible:
                    gap('unproved-compiler-baseline')
                    if settings:
                        gap('incompatible-compiler-baseline')
                # Any positive extension or alternate runtime ISA knob requires a
                # richer measured compiler adapter; unknown overrides fail closed.
                # GNU2 TLS selects the target ABI, not extra CPU instructions.
                extension_flags = re.sub(r'(?<!\S)-mtls-dialect=gnu2(?=\s|$)', '', flags)
                extensions = re.findall(r'(?<!\S)-m([^\s=]+)', extension_flags)
                if (any(value not in ('64', 'arch', 'cpu', 'tune') and not value.startswith('no-') for value in extensions) or
                        re.search(r'target-feature[^\s]*.*?\+', flags) or
                        re.search(r'GOAMD64=(?!v[12](?:\s|$))', flags) or
                        re.search(r'(?:-march=|-mcpu=|target-cpu=)[^\s]*\+', flags)):
                    gap('incompatible-compiler-baseline')
        dependencies = artifact.get('dependencyDigests')
        if (not isinstance(dependencies, list) or
                any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in dependencies) or
                len(dependencies) != len(set(dependencies))):
            gap('missing-dependency-closure')
        elif any(value not in by_digest for value in dependencies):
            gap('missing-dependency-artifact')
        machine, level = BASELINES[baseline]
        missing_static = []
        for elf in row['elfs']:
            if elf['machine'] != machine:
                gap('incompatible-elf-machine')
            if baseline == 'x86-64-v2' and elf['isaNeeded'] is not None and elf['isaNeeded'] > level:
                gap('incompatible-elf-isa')
            if elf['isaNeeded'] is None:
                missing_static.append(elf['digest'])
        if missing_static:
            execution = artifact.get('execution', {})
            if (not isinstance(execution, dict) or execution.get('artifactDigest') != identity or
                    execution.get('baseline') != baseline or type(execution.get('exitCode')) is not int or
                    execution.get('exitCode') != 0 or
                    not all(DIGEST.fullmatch(str(execution.get(field, ''))) for field in ('runnerDigest', 'evidenceDigest')) or
                    not isinstance(execution.get('elfDigests'), list) or
                    any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in execution.get('elfDigests', [])) or
                    not set(missing_static) <= set(execution['elfDigests'])):
                gap('missing-exact-restricted-execution')
        rows.append(row)
        blockers.extend({'artifactDigest': identity, 'code': code} for code in row['blockers'])
    # Resolve every dynamic NEEDED against actual inspected SONAME providers.
    # Native base libraries are supplied as digest-bound RPM artifacts too.
    inspected = {row['digest']: row for row in rows}
    for row in rows:
        artifact = by_digest[row['digest']]
        bindings = artifact.get('libraryProviders', {})
        dependencies = artifact.get('dependencyDigests', [])
        dependencies = dependencies if isinstance(dependencies, list) else []
        for elf in row['elfs']:
            for soname in elf.get('needed', []):
                candidates = [candidate['digest'] for candidate in rows
                              if any(lib.get('soname') == soname for lib in candidate['elfs'])]
                bound = bindings.get(soname) if isinstance(bindings, dict) else None
                if (not isinstance(bound, str) or not DIGEST.fullmatch(bound) or
                        bound not in dependencies or bound not in inspected or
                        candidates != [bound]):
                    code = 'unbound-or-ambiguous-linked-library'
                    if code not in row['blockers']:
                        row['blockers'].append(code)
                        blockers.append({'artifactDigest': row['digest'], 'code': code})
    return {'schemaVersion': 1, 'kind': 'cpu-baseline-inspection', 'baseline': baseline,
            'artifacts': sorted(rows, key=lambda row: row['digest']), 'blockers': blockers,
            'status': 'blocked' if blockers else 'evidence-bound', 'readiness': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input')
    parser.add_argument('--output')
    args = parser.parse_args()
    try:
        path = Path(args.input)
        if path.stat().st_size > LIMIT:
            raise BaselineError('input exceeds limit')
        result = verify(json.loads(path.read_bytes(), object_pairs_hook=pairs))
        text = json.dumps(result, indent=2, sort_keys=True) + '\n'
        if args.output:
            Path(args.output).write_text(text)
        else:
            print(text, end='')
        return 1 if result['blockers'] else 0
    except (BaselineError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({'status': 'blocked', 'error': str(exc), 'readiness': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
