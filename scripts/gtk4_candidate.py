"""Narrow source projection and actual upstream test admission; never readiness."""
import copy
import hashlib
import json
import math
from pathlib import Path

SCOPE = 'gtk4-provider'
SOURCE_SHA = '1ebb01ab14e98afd1727f68f64981c37bd23305b1f131f5667c02b94cf593192'
FILES = {'provider-admitted-tests.json', 'provider-test-receipt.json', 'testlog.json', 'testlog.txt'}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def project(recipe, target):
    need(target in {'alma10', 'alma10-kitten'}, 'unsupported GTK4 candidate target')
    need(recipe.get('name') == 'gtk4-layer-shell' and recipe.get('version') == '1.3.0'
         and recipe.get('targets') == ['el10'] and recipe.get('source', {}).get('sha256') == SOURCE_SHA,
         'GTK4 projection requires exact unactivated source contract')
    for kind in ('build', 'runtime'):
        section = recipe.get('dependencies', {}).get(kind)
        if kind == 'runtime' and section is None:
            continue
        targets = (section or {}).get('targets', {})
        need(target in targets and 'el10' in targets and targets[target] == targets['el10'],
             'GTK4 candidate native dependency projection missing or changed')
    projected = copy.deepcopy(recipe)
    projected['targets'] = [target]
    return projected


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'duplicate provider evidence identity')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite provider evidence')))


def admit_tests(directory, source_sha):
    directory = Path(directory)
    need(directory.is_dir() and not directory.is_symlink() and not any(parent.is_symlink() for parent in directory.parents)
         and source_sha == SOURCE_SHA,
         'wrong provider evidence ownership/source')
    need({p.name for p in directory.iterdir()} == FILES, 'provider evidence members missing or foreign')
    payloads = {}
    for name in FILES:
        path = directory / name
        need(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= 16*1024*1024,
             'unsafe provider evidence member')
        payloads[name] = path.read_bytes()
    admitted = strict_json(payloads['provider-admitted-tests.json'])
    need(isinstance(admitted, list), 'invalid provider admitted tests')
    names = [item.get('name') if isinstance(item, dict) else None for item in admitted]
    need(names and all(isinstance(name, str) and name for name in names)
         and len(names) == len(set(names)), 'invalid or duplicate provider tests')
    need(any(name.startswith('layer-') for name in names) and any(name.startswith('lock-') for name in names)
         and 'unit-test-get-version' in names, 'provider test scope incomplete')
    results = [strict_json(line) for line in payloads['testlog.json'].splitlines()]
    need(len(results) == len(names) and all(isinstance(test, dict) for test in results), 'provider results incomplete')
    need(all(isinstance(test.get('name'), str) for test in results), 'provider result name missing')
    need(sorted(test.get('name', '') for test in results) == sorted(names), 'provider results identity mismatch')
    for test in results:
        need(test.get('result') == 'OK' and type(test.get('returncode')) is int and test['returncode'] == 0,
             'provider test did not pass')
        command = test.get('command')
        need(isinstance(command, list) and command and all(isinstance(arg, str) for arg in command), 'provider command missing')
        for field, positive in (('starttime', True), ('duration', False)):
            value = test.get(field)
            need(type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0),
                 'provider timing missing')
    receipt = strict_json(payloads['provider-test-receipt.json'])
    expected = {'schema': 1, 'scope': 'upstream-mock-wayland-provider-tests', 'source_sha256': source_sha,
                'admitted_sha256': hashlib.sha256(payloads['provider-admitted-tests.json']).hexdigest(),
                'testlog_sha256': hashlib.sha256(payloads['testlog.json']).hexdigest(),
                'passed': sorted(names), 'hardware_qualified': False}
    need(receipt == expected and type(receipt.get('hardware_qualified')) is bool
         and type(receipt.get('schema')) is int, 'provider receipt does not bind actual tests')
    return {name: 'sha256:' + hashlib.sha256(data).hexdigest() for name, data in payloads.items()}
