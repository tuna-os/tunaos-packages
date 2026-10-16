"""Bounded requests to the public GitHub Actions API."""
from __future__ import annotations

import json
import re
import resource
import subprocess
import tempfile

API_VERSION = '2026-03-10'
MAX_METADATA = 4 * 1024 * 1024


def pairs(items):
    result = {}
    for key, value in items:
        if key in result: raise ValueError('duplicate API JSON field')
        result[key] = value
    return result


def api(endpoint):
    if not re.match(r'^repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/', endpoint) or any(
            character in endpoint for character in ('\\', '\n', '\r', '#')):
        raise ValueError('unapproved GitHub API endpoint')
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_METADATA, MAX_METADATA))
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(['gh', 'api', '--hostname', 'github.com', '-H',
                'X-GitHub-Api-Version: ' + API_VERSION, endpoint], stdout=output, stderr=errors,
                timeout=120, preexec_fn=limits)
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError('GitHub API request failed') from error
        if result.returncode: raise ValueError('GitHub API request failed')
        output.seek(0); raw = output.read(MAX_METADATA + 1)
    if len(raw) > MAX_METADATA: raise ValueError('GitHub API response exceeds limit')
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite API JSON')))
