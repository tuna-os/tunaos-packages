"""Explicit Alma recipes retain EL10 dependency expressions and payload settings.

Falsification: adding only a target name drops target-specific requirements;
reusing an EL10 artifact does not establish a native Alma build.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('tideforge', ROOT / 'scripts/tideforge.py')
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)
TRACKED = subprocess.check_output(
    ['git', 'ls-files', 'packages/**/package.yaml'], cwd=ROOT, text=True,
).splitlines()
RECIPES = [(path, yaml.safe_load((ROOT / path).read_text())) for path in TRACKED]
EL10 = [(path, recipe) for path, recipe in RECIPES if 'el10' in recipe.get('targets', [])]


def test_tracked_el10_coverage_is_complete():
    assert len(EL10) == 54
    assert 'packages/rwd/package.yaml' not in TRACKED
    for path, recipe in EL10:
        for target in ('alma10', 'alma10-kitten'):
            assert recipe['targets'].count(target) == 1, path


@pytest.mark.parametrize('path,recipe', EL10, ids=[path for path, _ in EL10])
def test_every_target_specific_map_preserves_exact_el10_value(path, recipe):
    def visit(value):
        if isinstance(value, dict):
            if 'el10' in value:
                for target in ('alma10', 'alma10-kitten'):
                    assert target in value, path
                    assert value[target] == value['el10'], path
                    assert value[target] is not value['el10'], path
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(recipe)


@pytest.mark.parametrize('path,recipe', EL10, ids=[path for path, _ in EL10])
def test_real_renderer_keeps_dependencies_verification_and_rpm_payload(path, recipe):
    expected_build = renderer.target_dependencies(recipe, 'el10')
    expected_runtime = renderer.target_runtime_dependencies(recipe, 'el10')
    expected_verify = renderer.verify_metadata(recipe, 'el10')
    expected_spec = renderer.render(recipe, 'el10')
    for target in ('alma10', 'alma10-kitten'):
        assert renderer.target_dependencies(recipe, target) == expected_build, path
        assert renderer.target_runtime_dependencies(recipe, target) == expected_runtime, path
        assert renderer.verify_metadata(recipe, target) == expected_verify, path
        actual_spec = renderer.render(recipe, target)
        for filename, reference in expected_spec.items():
            actual = actual_spec[filename]
            # Alma inserts measured compiler policy into %build; metadata,
            # preparation and installed payload remain identical.
            reference_head, reference_body = reference.split('%build\n', 1)
            actual_head, actual_body = actual.split('%build\n', 1)
            assert actual_head == reference_head, path
            assert actual_body.split('%install\n', 1)[1] == reference_body.split('%install\n', 1)[1], path
            assert 'tunaos_alma_compiler_policy %{_target_cpu}' in actual_body, path
            assert '%set_build_flags' in actual_body, path


def test_versioned_cosmic_requirements_are_not_reduced_to_names():
    recipe = yaml.safe_load((ROOT / 'packages/cosmic-session/package.yaml').read_text())
    for target in ('alma10', 'alma10-kitten'):
        runtime = renderer.target_runtime_dependencies(recipe, target)
        assert 'cosmic-comp >= 1.9.0' in runtime
        assert 'cosmic-settings-daemon >= 1.9.0' in runtime
        assert 'xdg-desktop-portal-cosmic >= 1.9.0' in runtime
        assert 'font(notosansmono)' in runtime
        assert 'system-cosmic-config' not in runtime
