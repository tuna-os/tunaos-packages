"""Alma builds must retain hardening while rejecting unproved CPU overrides.

Falsification: substitute native/v3 flags or reset compiler environment after the
policy; the renderer/helper must fail rather than emit a compatibility claim.
These tests establish policy behavior, not production package ISA compatibility.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location('alma_tideforge', ROOT / 'scripts/tideforge.py')
tideforge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tideforge)


@pytest.fixture
def recipe():
    return {'schema': 1, 'name': 'hello-tuna', 'version': '1', 'summary': 'Hello',
            'description': 'Test.', 'license': 'MIT', 'source': {'url': 'https://example.com/source.tar.gz', 'sha256': 'a'*64},
            'build_system': 'meson', 'dependencies': {'build': {'common': ['meson']}},
            'files': {'common': ['usr/bin/hello']}, 'targets': ['alma10', 'alma10-kitten', 'el10']}


def helper(tmp_path, architecture='x86_64', **flags):
    environment = {'PATH': os.environ['PATH'], 'TUNAOS_COMPILER_EVIDENCE_DIR': str(tmp_path), **flags}
    return subprocess.run(['bash', '-c', 'set -euo pipefail; source "$1"; tunaos_alma_compiler_policy "$2"',
                           'policy', str(ROOT / 'scripts/alma-compiler-policy.sh'), architecture],
                          env=environment, capture_output=True, text=True)


@pytest.mark.parametrize('architecture,baseline,rust,go', [('x86_64','x86-64-v2','x86-64-v2','v2'),
                                                         ('aarch64','armv8-a','generic',None)])
def test_real_helper_preserves_hardening_and_linkargs_and_records_flags(tmp_path, architecture, baseline, rust, go):
    result = helper(tmp_path, architecture, CFLAGS='-O2 -g -fstack-protector-strong',
                    CXXFLAGS='-O2 -D_FORTIFY_SOURCE=3', RUSTFLAGS='-C link-arg=-Wl,-z,relro',
                    LDFLAGS='-Wl,-z,now')
    assert result.returncode == 0, result.stderr
    evidence = json.loads((tmp_path/'effective-flags.json').read_text())
    assert evidence['cpuBaseline'] == baseline
    assert evidence['flags']['CFLAGS'] == '-O2 -g -fstack-protector-strong -march='+baseline
    assert evidence['flags']['RUSTFLAGS'] == '-C link-arg=-Wl,-z,relro -C target-cpu='+rust
    assert evidence['flags']['LDFLAGS'] == '-Wl,-z,now'
    assert evidence['flags']['GOAMD64'] == go
    assert evidence['readiness'] is False


@pytest.mark.parametrize('flags', [{'CFLAGS':'-march=native'}, {'CXXFLAGS':'-march=x86-64-v3'},
    {'CFLAGS':'-march=broadwell'}, {'CFLAGS':'-mavx2'}, {'RUSTFLAGS':'-C target-feature=+avx'},
    {'CARGO_ENCODED_RUSTFLAGS':'-C\x1ftarget-cpu=native'}, {'GOAMD64':'v3'}, {'CC':'gcc -march=native'}])
def test_real_helper_rejects_higher_unknown_or_encoded_overrides(tmp_path, flags):
    assert helper(tmp_path, **flags).returncode != 0
    assert not (tmp_path/'effective-flags.json').exists()


@pytest.mark.parametrize('target', ['alma10', 'alma10-kitten'])
def test_renderer_policy_runs_after_recipe_exports_before_actual_build(recipe, target):
    recipe['build'] = {'environment': {'CFLAGS':'-O2 -fstack-protector-strong',
                                       'RUSTFLAGS':'-C link-arg=-Wl,-z,relro'}}
    rendered = tideforge.render_rpm(recipe, target)['hello-tuna.spec']
    build = rendered.split('\n%build\n',1)[1].split('\n%install\n',1)[0]
    assert build.index('%set_build_flags') < build.index('export CFLAGS=')
    assert build.index('export CFLAGS=') < build.index('tunaos_alma_compiler_policy %{_target_cpu}') < build.index('%meson ')
    assert 'link-arg=-Wl,-z,relro' in build
    assert recipe['build']['environment']['CFLAGS'] == '-O2 -fstack-protector-strong'


@pytest.mark.parametrize('commands', ['gcc -march=native test.c', 'export CFLAGS=-O2',
    'CFLAGS=-O2 make', 'gcc -mavx2 test.c', 'GOAMD64=v3 go build', 'cargo rustc -- -C target-cpu=native'])
def test_renderer_rejects_authored_commands_that_override_policy(recipe, commands):
    recipe['build_system'] = 'custom'
    recipe['build'] = {'commands': [commands]}
    with pytest.raises(SystemExit):
        tideforge.render_rpm(recipe, 'alma10')


def test_non_alma_render_output_matches_committed_renderer(recipe):
    source = subprocess.run(['git','show','HEAD:scripts/tideforge.py'], cwd=ROOT,
                            check=True,capture_output=True,text=True).stdout
    namespace = {'__file__': str(ROOT/'scripts/tideforge.py'), '__name__':'baseline_renderer'}
    exec(compile(source, 'committed_tideforge', 'exec'), namespace)
    assert tideforge.render_rpm(recipe,'el10') == namespace['render_rpm'](recipe,'el10')


def test_actual_arm_effective_flags_are_accepted_by_cpu_verifier(tmp_path):
    assert helper(tmp_path, 'aarch64', CFLAGS='-O2').returncode == 0
    effective = json.loads((tmp_path/'effective-flags.json').read_text())
    spec = importlib.util.spec_from_file_location('alma_cpu_verifier', ROOT/'scripts/verify-cpu-baseline.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    path = tmp_path/'artifact.rpm'
    path.write_bytes(b'fixture')
    digest = verifier.digest(path.read_bytes())
    document = {'schemaVersion':1,'baseline':'armv8-a','artifacts':[{'path':str(path),'digest':digest,
        'dependencyDigests':[],'compiler':{'artifactDigest':digest,'evidenceDigest':'sha256:'+'a'*64,
        'flags':[effective['flags']['CFLAGS'],effective['flags']['RUSTFLAGS']]}}]}
    result = verifier.verify(document,extract=lambda path:[])
    assert result['status'] == 'evidence-bound', result['blockers']


def test_guard_failure_is_retained_when_helper_called_inside_shell_conditional(tmp_path):
    environment = {'PATH':os.environ['PATH'],'TUNAOS_COMPILER_EVIDENCE_DIR':str(tmp_path),'CFLAGS':'-march=native'}
    result = subprocess.run(['bash','-c','source "$1"; if tunaos_alma_compiler_policy x86_64; then exit 42; else exit 0; fi',
                             'policy',str(ROOT/'scripts/alma-compiler-policy.sh')],env=environment,capture_output=True,text=True)
    assert result.returncode == 0
    assert not (tmp_path/'effective-flags.json').exists()


def test_actual_rpm_hardening_m64_is_retained(tmp_path):
    result = helper(tmp_path, CFLAGS='-O2 -g -pipe -m64 -fstack-protector-strong -fstack-clash-protection -fcf-protection')
    assert result.returncode == 0, result.stderr
    actual = json.loads((tmp_path/'effective-flags.json').read_text())['flags']['CFLAGS']
    assert actual == '-O2 -g -pipe -m64 -fstack-protector-strong -fstack-clash-protection -fcf-protection -march=x86-64-v2'


# Exact measured optflags from alma10-v2-compiler-probe.log:214, excluding
# the two leading RPM architecture fields. This is behavior evidence only.
ALMA_OPTFLAGS = ('-O2 -flto=auto -ffat-lto-objects -fexceptions -g -grecord-gcc-switches '
    '-pipe -Wall -Wno-complain-wrong-lang -Werror=format-security '
    '-Wp,-U_FORTIFY_SOURCE,-D_FORTIFY_SOURCE=3 -Wp,-D_GLIBCXX_ASSERTIONS '
    '-specs=/usr/lib/rpm/redhat/redhat-hardened-cc1 -fstack-protector-strong '
    '-specs=/usr/lib/rpm/redhat/redhat-annobin-cc1  -m64 -march=x86-64-v2 '
    '-mtune=generic -fasynchronous-unwind-tables -fstack-clash-protection '
    '-fcf-protection -mtls-dialect=gnu2 -fno-omit-frame-pointer -mno-omit-leaf-frame-pointer')


def test_full_measured_alma_optflags_survive_helper_and_verifier(tmp_path):
    result = helper(tmp_path, CFLAGS=ALMA_OPTFLAGS, RUSTFLAGS='-C link-arg=-Wl,-z,relro')
    assert result.returncode == 0, result.stderr
    effective = json.loads((tmp_path/'effective-flags.json').read_text())
    assert effective['flags']['CFLAGS'] == ALMA_OPTFLAGS + ' -march=x86-64-v2'
    spec = importlib.util.spec_from_file_location('full_optflags_cpu',ROOT/'scripts/verify-cpu-baseline.py')
    verifier=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    path=tmp_path/'fixture.rpm'
    path.write_bytes(b'fixture')
    identity=verifier.digest(path.read_bytes())
    document={'schemaVersion':1,'baseline':'x86-64-v2','artifacts':[{'path':str(path),'digest':identity,
        'dependencyDigests':[],'compiler':{'artifactDigest':identity,'evidenceDigest':'sha256:'+'a'*64,
        'flags':[effective['flags']['CFLAGS'],effective['flags']['RUSTFLAGS']]}}]}
    assert verifier.verify(document,extract=lambda path:[])['status']=='evidence-bound'


@pytest.mark.parametrize('flag',['-mtls-dialect=gnu','-mtls-dialect=gnu2-extra','-mavx','-mavx2'])
def test_unapproved_tls_or_isa_flags_still_block(tmp_path,flag):
    assert helper(tmp_path,CFLAGS=ALMA_OPTFLAGS+' '+flag).returncode!=0


def test_renderer_accepts_non_cpu_tls_abi_option_in_custom_commands(recipe):
    recipe['build_system']='custom'
    recipe['build']={'commands':['gcc -mtls-dialect=gnu2 hello.c -o hello']}
    recipe['install']={'commands':['install -Dm0755 hello {destdir}/usr/bin/hello']}
    assert 'gcc -mtls-dialect=gnu2' in tideforge.render_rpm(recipe,'alma10')['hello-tuna.spec']
