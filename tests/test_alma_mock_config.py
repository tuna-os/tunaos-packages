"""Alma buildroots must resolve signed native inputs for the actual CPU family.

Falsification: inheriting the existing CentOS/EPEL x86 config silently introduces
v3 dependencies; setting target_arch alone cannot make those artifacts v2.
These tests inspect compiler/repository inputs, not artifact ISA proof.
"""
from __future__ import annotations

import configparser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    (release, arch, family)
    for release in ('alma10', 'alma10-kitten')
    for arch in ('x86_64', 'aarch64')
    for family in ('rpm', 'gnome50', 'gnome51', 'xfce')
]


def load_config(release, arch, family):
    name = release + '-ci'
    if family != 'rpm':
        name += '-' + family
    if arch == 'aarch64':
        name += '-aarch64'
    options = {}

    def include(path):
        # Mock's external config include boundary; execute real checked-in files.
        assert not Path(path).is_absolute(), 'include bypasses checkout --configdir'
        source = ROOT / 'mock' / path
        assert source.is_file(), source
        exec(compile(source.read_text(), str(source), 'exec'), environment)

    environment = {'config_opts': options, 'include': include}
    include(name + '.cfg')
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(options['dnf.conf'])
    return name, options, parser


@pytest.mark.parametrize('release,arch,family', CASES)
def test_native_architecture_and_compiler_floor(release, arch, family):
    name, options, _ = load_config(release, arch, family)
    assert options['root'] == name
    assert options['target_arch'] == arch
    assert options['legal_host_arches'] == (arch,)
    assert options['macros']['%_target_cpu'] == arch
    assert '%optflags' not in options['macros'], 'short resets discard vendor hardening'
    assert options['rpmbuild_command'] == '/usr/bin/python3 /keys/alma-rpmbuild-guard.py ' + arch
    assert 'python3' in options['chroot_setup_cmd'].split()
    assert options['use_bootstrap'] is False  # no unpinned image escape


@pytest.mark.parametrize('release,arch,family', CASES)
def test_bootstrap_release_package_exists_in_the_native_release_namespace(release, arch, family):
    # CI Kitten bootstrap failed on stable's almalinux-release. Official
    # Kitten BaseOS publishes almalinux-kitten-release on v2 and ARM alike:
    # https://kitten.repo.almalinux.org/10-kitten/BaseOS/x86_64_v2/os/Packages/
    # https://kitten.repo.almalinux.org/10-kitten/BaseOS/aarch64/os/Packages/
    _, options, _ = load_config(release, arch, family)
    packages = options['chroot_setup_cmd'].split()[1:]
    expected = 'almalinux-kitten-release' if release == 'alma10-kitten' else 'almalinux-release'
    assert expected in packages
    other = 'almalinux-release' if release == 'alma10-kitten' else 'almalinux-kitten-release'
    assert other not in packages


@pytest.mark.parametrize('release,arch,family', CASES)
def test_native_repositories_and_signing_keys(release, arch, family):
    _, _, repos = load_config(release, arch, family)
    host = ('https://repo.almalinux.org/almalinux/10' if release == 'alma10'
            else 'https://kitten.repo.almalinux.org/10-kitten')
    repo_arch = 'x86_64_v2' if arch == 'x86_64' else 'aarch64'
    for section, directory in [('baseos', 'BaseOS'), ('appstream', 'AppStream'), ('crb', 'CRB')]:
        assert repos[section]['baseurl'] == f'{host}/{directory}/{repo_arch}/os/'
        assert repos[section]['gpgkey'] == 'https://repo.almalinux.org/almalinux/RPM-GPG-KEY-AlmaLinux-10'
    branch = '10z' if release == 'alma10' else '10'
    if arch == 'x86_64':
        epel = repos['alma-epel-v2']
        assert epel['baseurl'] == f'https://epel.repo.almalinux.org/{branch}/x86_64_v2/'
        assert epel['gpgkey'] == 'https://repo.almalinux.org/almalinux/RPM-GPG-KEY-AlmaLinux-10-EPEL-AltArch'
        assert 'fedoraproject.org' not in str(dict(epel))
    else:
        epel = repos['epel-arm']
        assert epel['baseurl'] == f'https://dl.fedoraproject.org/pub/epel/{branch}/Everything/aarch64/'
        assert epel['gpgkey'] == 'https://dl.fedoraproject.org/pub/epel/RPM-GPG-KEY-EPEL-10'
        assert 'x86_64' not in str(dict(epel))


@pytest.mark.parametrize('release,arch,family', CASES)
def test_every_enabled_input_requires_signatures_and_missing_inputs_fail(release, arch, family):
    _, options, repos = load_config(release, arch, family)
    assert repos['main']['gpgcheck'] == '1'
    assert repos['main']['localpkg_gpgcheck'] == '1'
    assert repos['main']['reposdir'] == '/dev/null'
    for section in repos.sections():
        if section == 'main':
            continue
        repo = repos[section]
        assert repo['enabled'] == ('0' if section.startswith('tunaos-') else '1')
        assert repo['gpgcheck'] == '1', section
        assert repo['skip_if_unavailable'] == '0', section
        assert repo['gpgkey'], section
    assert ('/keys', '/keys') in options['plugin_conf']['bind_mount_opts']['dirs']
    assert repos['local-build']['gpgkey'] == 'file:///keys/candidate-public.gpg'
    assert options['rpmbuild_networking'] is False
    assert options['use_host_resolv'] is False


@pytest.mark.parametrize('release,arch,family', CASES)
def test_family_namespace_and_source_priority_are_isolated(release, arch, family):
    _, _, repos = load_config(release, arch, family)
    factory = repos['tunaos-' + family]
    expected = (f'https://repo.tunaos.org/rpm/{release}/{arch}/' if family == 'rpm'
                else f'https://repo.tunaos.org/{family}/{release}-{arch}/')
    assert factory['baseurl'] == expected
    assert factory['gpgkey'] == 'file:///keys/tunaos-public.gpg'
    assert repos['local-build']['baseurl'] == 'file:///local-repo/'
    assert repos['local-build']['priority'] == '1'
    assert factory['priority'] == '11'
    assert repos['baseos']['priority'] == '50'
    assert len([s for s in repos.sections() if s.startswith('tunaos-')]) == 1
    for section in repos.sections():
        assert repos[section]['excludepkgs'] == 'icu77*'
    text = '\n'.join(str(dict(repos[s])) for s in repos.sections()).lower()
    for forbidden in ('centos', 'kojihub', 'kojipkgs', 'copr', '/sigs/', '10-stream', 'icu74*'):
        assert forbidden not in text


def test_all_sixteen_chroot_caches_have_unique_roots():
    names = [load_config(*case)[0] for case in CASES]
    assert len(names) == 16
    assert len(set(names)) == 16


@pytest.mark.parametrize('release,arch,family', CASES)
def test_first_wave_uses_signed_local_and_native_inputs_without_future_http_index(release, arch, family):
    _, _, repos = load_config(release, arch, family)
    enabled = {section for section in repos.sections()
               if section != 'main' and repos[section]['enabled'] == '1'}
    epel = 'alma-epel-v2' if arch == 'x86_64' else 'epel-arm'
    assert enabled == {'baseos', 'appstream', 'crb', epel, 'local-build'}
    assert repos['local-build']['gpgkey'] != repos['tunaos-' + family]['gpgkey']
    assert repos['local-build']['skip_if_unavailable'] == '0'
    assert repos['tunaos-' + family]['skip_if_unavailable'] == '0'


@pytest.mark.parametrize('release,arch,family', [case for case in CASES if case[2] != 'rpm'])
def test_family_include_resolves_exact_checkout_generic_config(release, arch, family):
    name, options, _ = load_config(release, arch, family)
    generic = release + '-ci' + ('-aarch64' if arch == 'aarch64' else '') + '.cfg'
    source = (ROOT / 'mock' / (name + '.cfg')).read_text()
    assert "include('" + generic + "')" in source
    assert '/etc/mock/' not in '\n'.join(line for line in source.splitlines()
                                         if not line.lstrip().startswith('#'))
    # The inherited setting establishes that the real checkout generic executed.
    assert options['macros']['%_target_cpu'] == arch
