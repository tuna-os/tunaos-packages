"""Public GitHub API process limits, exercised by real subprocess fixtures."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('github_api_boundary', ROOT / 'scripts/github_api.py')
github_api = importlib.util.module_from_spec(spec); spec.loader.exec_module(github_api)


@pytest.fixture
def gh_boundary(tmp_path, monkeypatch):
    executable = tmp_path / 'gh'
    executable.write_text('#!' + sys.executable + '''
import os, sys
assert sys.argv[1:4] == ['api', '--hostname', 'github.com']
assert 'X-GitHub-Api-Version: 2026-03-10' in sys.argv
if os.environ.get('API_OVERSIZED'):
    while True: os.write(1, b'x' * 65536)
print('{"id":123}')
''')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + ':' + os.environ['PATH'])


def test_api_uses_public_host_and_current_version(gh_boundary):
    assert github_api.api('repos/tuna-os/tunaos-packages/actions/runs/123') == {'id': 123}


def test_api_os_limit_stops_oversized_child_output(gh_boundary, monkeypatch):
    monkeypatch.setenv('API_OVERSIZED', '1')
    with pytest.raises(ValueError, match='GitHub API request failed'):
        github_api.api('repos/tuna-os/tunaos-packages/actions/runs/123')


@pytest.mark.parametrize('endpoint', ['https://evil.example/', 'repos/example/repo/issues',
                                     'repos/example/repo/actions/runs/1\n', 'repos/example/repo/actions/#fragment'])
def test_api_rejects_unapproved_endpoints_before_execution(endpoint):
    with pytest.raises(ValueError, match='unapproved'):
        github_api.api(endpoint)
