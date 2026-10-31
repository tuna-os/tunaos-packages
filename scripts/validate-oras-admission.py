#!/usr/bin/env python3
"""Prevent legacy publication from overwriting an ORAS-admitted cell."""
import json
import os
import re


def validate(cells, transport, enabled, approved, seeds):
    if not isinstance(approved, list) or not all(isinstance(c, str) for c in approved):
        raise ValueError('ORAS_PRODUCTION_CELLS must be a JSON list of cell IDs')
    if transport not in {'r2', 'oras'}:
        raise ValueError('unknown distribution transport')
    if transport == 'r2':
        if set(cells) & set(approved):
            raise ValueError('ORAS-admitted cells cannot be seeded/published from R2')
        return
    if enabled != 'true' or not set(cells) <= set(approved):
        raise ValueError('ORAS production requires explicit admission of every cell')
    if not isinstance(seeds, dict) or set(seeds) != set(cells):
        raise ValueError('exactly one seed per selected cell is required')
    for seed in seeds.values():
        if not isinstance(seed, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', seed):
            raise ValueError('every seed must be an immutable sha256 digest')


if __name__ == '__main__':
    validate([c.strip() for c in os.environ['CELLS'].split(',') if c.strip()],
             os.environ.get('DISTRIBUTION') or 'r2', os.environ.get('ORAS_ENABLED', ''),
             json.loads(os.environ.get('ORAS_CELLS') or '[]'),
             json.loads(os.environ.get('ORAS_SEEDS') or '{}'))
