"""INCIDENT-Go-inventory: Go recipes stopped before compilation with exit 2.

Falsification: `go --version` is not a valid Go command. Recording installed
compiler identity must use the native command and must retain genuine errors.
"""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('broken', [False, True])
def test_real_inventory_loop_uses_native_go_version_and_keeps_errors(tmp_path, broken):
    tools = tmp_path / 'bin'
    tools.mkdir()
    output = tmp_path / 'evidence'
    output.mkdir()
    for name in ('gcc', 'clang', 'rustc', 'cargo', 'go'):
        command = tools / name
        expected = 'version' if name == 'go' else '--version'
        command.write_text('#!/bin/sh\n'
                           f'[ "$1" = "{expected}" ] || exit 2\n'
                           + ('exit 3\n' if name == 'go' and broken else f'echo "{name} native-version"\n'))
        command.chmod(0o755)
    source = (ROOT / 'scripts/run-alma-rpm.sh').read_text()
    start = source.index('for compiler in gcc clang rustc cargo go; do')
    end = source.index('\ndone', start) + len('\ndone')
    loop = source[start:end].replace('/work/evidence/', str(output) + '/')
    result = subprocess.run(['/bin/bash', '-c', 'set -euo pipefail\n' + loop],
                            env={**os.environ, 'PATH': str(tools)}, capture_output=True, text=True)
    assert result.returncode == (3 if broken else 0)
    if not broken:
        assert (output / 'go-version.txt').read_text() == 'go native-version\n'
    assert (output / 'gcc-version.txt').read_text() == 'gcc native-version\n'
