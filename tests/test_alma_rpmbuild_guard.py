"""Actual-chroot RPM boundary regressions; these fixtures are not CPU proof.

INCIDENT-Alma-hardening: a short optflags reset dropped native vendor defenses.
Falsification: missing/higher ISA macros or alternate RPM inputs must prevent
the real rpmbuild process, while measured vendor hardening survives unchanged.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('alma_rpmbuild_guard', ROOT / 'scripts/alma-rpmbuild-guard.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

X86_FLAGS = ('-O2 -flto=auto -ffat-lto-objects -fexceptions -g -grecord-gcc-switches '
             '-pipe -Wall -Wno-complain-wrong-lang -Werror=format-security '
             '-Wp,-U_FORTIFY_SOURCE,-D_FORTIFY_SOURCE=3 -Wp,-D_GLIBCXX_ASSERTIONS '
             '-specs=/usr/lib/rpm/redhat/redhat-hardened-cc1 -fstack-protector-strong '
             '-specs=/usr/lib/rpm/redhat/redhat-annobin-cc1 -m64 -march=x86-64-v2 '
             '-mtune=generic -fasynchronous-unwind-tables -fstack-clash-protection '
             '-fcf-protection -mtls-dialect=gnu2 -fno-omit-frame-pointer -mno-omit-leaf-frame-pointer')
ARM_FLAGS = ('-O2 -g -D_FORTIFY_SOURCE=3 -fstack-protector-strong '
             '-march=armv8-a -mbranch-protection=standard -mno-omit-leaf-frame-pointer')
LINK_FLAGS = '-Wl,-z,relro -Wl,-z,now -specs=/usr/lib/rpm/redhat/redhat-hardened-ld'


@pytest.fixture
def boundary(monkeypatch):
    values = {'_target_cpu': 'x86_64', 'optflags': X86_FLAGS,
              'build_cflags': X86_FLAGS, 'build_cxxflags': X86_FLAGS, 'build_ldflags': LINK_FLAGS}
    commands, executions = [], []
    changed = {}

    def rpm(command, **kwargs):
        commands.append(command)
        assert command[0] == '/usr/bin/rpm'
        assert kwargs == {'check': True, 'capture_output': True, 'text': True, 'timeout': 30}
        field = command[-1][2:-1]
        assert command[-2] == '--eval'
        value = changed.get(field, values[field]) if len(command) > 3 else values[field]
        return SimpleNamespace(stdout=value + '\n')

    monkeypatch.setattr(subprocess, 'run', rpm)
    monkeypatch.setattr(guard.os, 'execv', lambda path, arguments: executions.append((path, arguments)))
    return values, changed, commands, executions


@pytest.mark.parametrize('architecture,flags', [('x86_64', X86_FLAGS), ('aarch64', ARM_FLAGS)])
def test_native_vendor_flags_are_measured_before_exact_rpmbuild_and_never_reset(boundary, capsys, architecture, flags):
    values, _, commands, executions = boundary
    values.update(_target_cpu=architecture, optflags=flags, build_cflags=flags, build_cxxflags=flags)
    arguments = ['-bb', '--target', architecture, '--define', 'dist .el10.alma', '/builddir/build/SPECS/evtest.spec']
    guard.main([architecture, *arguments])
    assert executions == [('/usr/bin/rpmbuild', ['/usr/bin/rpmbuild', *arguments])]
    assert len(commands) == 10
    assert commands[0] == ['/usr/bin/rpm', '--eval', '%{_target_cpu}']
    assert commands[5] == ['/usr/bin/rpm', '--target', architecture, '--define', 'dist .el10.alma', '--eval', '%{_target_cpu}']
    line = capsys.readouterr().out
    assert line.startswith('TUNAOS_ALMA_RPMBUILD_GUARD ')
    evidence = json.loads(line.split(' ', 1)[1])
    assert evidence['macros'] == values
    assert evidence['arguments'] == arguments
    assert evidence['cpuBaseline'] == ('x86-64-v2' if architecture == 'x86_64' else 'armv8-a')
    assert evidence['readiness'] is False


@pytest.mark.parametrize('field,value', [
    ('_target_cpu', 'aarch64'), ('optflags', ''), ('build_cflags', '%{build_cflags}'),
    ('build_cxxflags', '-O2 -g'), ('build_ldflags', '-Wl,-z,relro\nadditional-output'),
    ('optflags', X86_FLAGS + ' -march=x86-64-v3'),
    ('build_cflags', X86_FLAGS + ' -march=native'),
    ('build_cxxflags', X86_FLAGS + ' -mavx2'),
    ('build_ldflags', LINK_FLAGS + ' -march=x86-64-v3'),
    ('optflags', X86_FLAGS + ' -mcpu=native'),
    ('build_cflags', "'unterminated"),
])
def test_missing_conflicting_or_unproved_actual_chroot_macros_block_build(boundary, field, value):
    values, _, _, executions = boundary
    values[field] = value
    with pytest.raises(ValueError):
        guard.main(['x86_64', '-bb', '/builddir/build/SPECS/evtest.spec'])
    assert executions == []


@pytest.mark.parametrize('field', ['optflags', 'build_cflags', 'build_cxxflags', 'build_ldflags'])
def test_cli_indirect_macro_override_cannot_strip_vendor_hardening(boundary, field):
    _, changed, _, executions = boundary
    changed[field] = '-O2 -march=x86-64-v2' if field != 'build_ldflags' else '-Wl,-z,relro'
    with pytest.raises(ValueError, match='alter native vendor compiler macros'):
        guard.main(['x86_64', '-bb', '--define', '_hardened_build 0', 'evtest.spec'])
    assert executions == []


@pytest.mark.parametrize('option', [
    '--rcfile=/tmp/other', '--macros', '--root=/other', '--undefine=optflags',
    '--load=/tmp/macros', '--eval', '-E%{optflags}', '--def', '-r', '--dbpath=/tmp/rpmdb',
])
def test_alternate_or_abbreviated_rpm_inputs_fail_before_query_or_build(boundary, option):
    _, _, commands, executions = boundary
    with pytest.raises(ValueError):
        guard.main(['x86_64', '-bb', option, 'evtest.spec'])
    assert commands == []
    assert executions == []


@pytest.mark.parametrize('arguments', [[], ['ppc64le', '-bb'], ['x86_64', '--define'], ['x86_64', '--target']])
def test_missing_or_unknown_context_is_not_an_implicit_generic_build(boundary, arguments):
    with pytest.raises(ValueError):
        guard.main(arguments)
    assert boundary[3] == []


def test_rpm_observation_failure_cannot_start_build(boundary, monkeypatch):
    def unavailable(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(subprocess, 'run', unavailable)
    with pytest.raises(subprocess.CalledProcessError):
        guard.main(['x86_64', '-bb', 'evtest.spec'])
    assert boundary[3] == []


@pytest.mark.parametrize('flags', [ARM_FLAGS + ' -march=armv8.3-a', ARM_FLAGS + ' -mbranch-protection=standard+leaf'])
def test_arm_hardening_permission_cannot_change_the_native_floor(boundary, flags):
    values, _, _, executions = boundary
    values.update(_target_cpu='aarch64', optflags=flags, build_cflags=flags, build_cxxflags=flags)
    with pytest.raises(ValueError):
        guard.main(['aarch64', '-bb', 'evtest.spec'])
    assert executions == []
