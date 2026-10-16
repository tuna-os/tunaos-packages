#!/usr/bin/python3
"""Check native chroot RPM flags before building; never replace vendor flags."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

MACROS = ('_target_cpu', 'optflags', 'build_cflags', 'build_cxxflags', 'build_ldflags')
BASELINES = {'x86_64': 'x86-64-v2', 'aarch64': 'armv8-a'}


def macro_options(arguments):
    """Evaluate the same explicit macro/target options; forbid alternate inputs."""
    options = []
    position = 0
    while position < len(arguments):
        token = arguments[position]
        position += 1
        if token in ('--define', '-D', '--target'):
            if position == len(arguments):
                raise ValueError('missing RPM macro/target argument')
            options.extend((token, arguments[position]))
            position += 1
        elif token.startswith(('--define=', '--target=')):
            name, value = token.split('=', 1)
            options.extend((name, value))
        elif token.startswith('-D') and token != '-D':
            options.extend(('-D', token[2:]))
        elif token.startswith(('--undefine', '--macros', '--rcfile', '--root', '--dbpath', '--load', '--eval')) or token == '-E':
            raise ValueError('alternate RPM macro/configuration inputs are forbidden')
        elif token.startswith('--') and token.split('=', 1)[0] not in {
            '--nodeps', '--nocheck', '--noclean', '--short-circuit', '--clean',
            '--rmsource', '--rmspec', '--quiet', '--verbose', '--with', '--without',
        }:
            raise ValueError('unsupported rpmbuild option: ' + token)
        elif token.startswith('-') and not token.startswith('--') and token not in {
            '-ba', '-bb', '-bs', '-bp', '-bc', '-bi', '-bl', '-br', '-bd', '-bf', '-bk', '-v', '-q',
        }:
            raise ValueError('unsupported rpmbuild option: ' + token)
    return options


def observed_macros(options):
    result = {}
    for name in MACROS:
        value = subprocess.run(['/usr/bin/rpm', *options, '--eval', '%{' + name + '}'],
                               check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        if not value or '%{' in value or '\n' in value:
            raise ValueError('missing or unresolved native RPM macro: ' + name)
        result[name] = value
    return result


def validate_flags(macros, architecture, require_baseline=True):
    baseline = BASELINES.get(architecture)
    if baseline is None or macros['_target_cpu'] != architecture:
        raise ValueError('native RPM target CPU disagrees with Alma target')
    for name in MACROS[1:]:
        tokens = shlex.split(macros[name])
        for token in tokens:
            if baseline == 'armv8-a' and token == '-mbranch-protection=standard':
                continue
            for setting in re.findall(r'(?:-march=|-mcpu=|target-cpu=)([^\s]+)', token):
                if setting != baseline:
                    raise ValueError('incompatible native RPM CPU flag: ' + token)
            if re.search(r'(?<!\w)-m(?!arch=|cpu=|tune=|64$|no-|tls-dialect=gnu2$)[a-zA-Z]', token):
                raise ValueError('unproved native RPM CPU extension: ' + token)
        if require_baseline and name != 'build_ldflags' and '-march=' + baseline not in tokens:
            raise ValueError('native RPM flags lack explicit Alma baseline: ' + name)


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if not arguments or arguments[0] not in BASELINES:
        raise ValueError('expected Alma target architecture is required')
    architecture, arguments = arguments[0], arguments[1:]
    options = macro_options(arguments)
    vendor = observed_macros([])
    effective = observed_macros(options)
    validate_flags(vendor, architecture, require_baseline=False)
    validate_flags(effective, architecture, require_baseline=False)
    if vendor != effective:
        raise ValueError('rpmbuild arguments alter native vendor compiler macros')
    # Native ARM vendor flags need not spell out GCC's default ISA. Append
    # only its explicit baseline; never reset hardening or mask a higher ISA.
    expected = dict(effective)
    injected = []
    for name in ('optflags', 'build_cflags', 'build_cxxflags'):
        baseline_flag = '-march=' + BASELINES[architecture]
        if baseline_flag not in shlex.split(effective[name]):
            expected[name] = effective[name] + ' ' + baseline_flag
            injected.extend(('--define', name + ' ' + expected[name]))
    if injected:
        effective = observed_macros([*options, *injected])
        if effective != expected:
            raise ValueError('baseline injection changed native vendor hardening')
    validate_flags(effective, architecture)
    # Mock captures this immutable observation in build.log before rpmbuild.
    print('TUNAOS_ALMA_RPMBUILD_GUARD ' + json.dumps({
        'schemaVersion': 1, 'architecture': architecture,
        'cpuBaseline': BASELINES[architecture], 'vendorMacros': vendor, 'macros': effective,
        'arguments': arguments, 'baselineDefinitions': injected, 'readiness': False,
    }, sort_keys=True), flush=True)
    os.execv('/usr/bin/rpmbuild', ['/usr/bin/rpmbuild', *arguments, *injected])


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.exit('Alma native rpmbuild guard: ' + str(error))
