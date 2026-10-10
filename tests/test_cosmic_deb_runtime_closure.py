"""A session package must demand the runtime it starts.

Falsification: restoring an empty Debian Depends or dropping a queue sibling
must fail even when the session binary itself builds and its smoke test passes.
Actual signed consumer installation remains a separate CI acceptance gate.
"""
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import tideforge

SIBLINGS = {
    'cosmic-app-library', 'cosmic-applets', 'cosmic-bg', 'cosmic-comp',
    'cosmic-files', 'cosmic-greeter', 'cosmic-icon-theme', 'cosmic-idle',
    'cosmic-initial-setup', 'cosmic-launcher', 'cosmic-notifications',
    'cosmic-osd', 'cosmic-osk', 'cosmic-panel', 'cosmic-randr',
    'cosmic-screenshot', 'cosmic-settings', 'cosmic-settings-daemon',
    'cosmic-term', 'cosmic-workspaces', 'xdg-desktop-portal-cosmic',
}


def recipe(name):
    return yaml.safe_load((ROOT / f'packages/{name}/package.yaml').read_text())


@pytest.mark.parametrize('target', ['ubuntu', 'debian'])
def test_actual_deb_control_requires_complete_versioned_session(target):
    session = recipe('cosmic-session')
    expected = {f'{name} (>= 1.9.0)' for name in SIBLINGS}
    runtime = set(tideforge.target_runtime_dependencies(session, target))
    assert expected <= runtime
    assert {'fonts-open-sans', 'fonts-noto-mono', 'xdg-user-dirs'} <= runtime
    control = tideforge.render_deb(session, target)['debian/control']
    depends = next(line for line in control.splitlines() if line.startswith('Depends:'))
    for expression in expected:
        assert expression in depends


@pytest.mark.parametrize('target', ['ubuntu', 'debian'])
def test_queue_contains_all_actual_session_and_launcher_providers(target):
    queue = yaml.safe_load((ROOT / 'manifests/target-queues/cosmic.yaml').read_text())['queues'][target]
    assert SIBLINGS | {'cosmic-session', 'pop-launcher', 'pop-icon-theme'} <= set(queue['roots'])
    assert {'apt-stage-install', 'greetd-login', 'cosmic-session-smoke'} <= set(queue['gates'])
    launcher = recipe('cosmic-launcher')
    runtime = tideforge.target_runtime_dependencies(launcher, target)
    assert set(runtime) == {'pop-launcher', 'cosmic-icon-theme (>= 1.9.0)'}
    assert 'pop-launcher' in tideforge.render_deb(launcher, target)['debian/control']
