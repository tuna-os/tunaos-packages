"""The deb verify job must hand apt a key it can read (#951).

public.gpg is ASCII-armored. apt's `signed-by=` ignores an armored key in a
file named `.gpg` ("unsupported filetype"), and `gpg --keyring` cannot read it
either, so every verify job failed at `apt-get update` with NO_PUBKEY. This
runs the verify step up to `docker run` and checks the file it mounts at
$KEYRING.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "publish-tideforge-debs.yml"
STEP = "Install from the published repo with no third-party sources"


@pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not installed")
def test_the_keyring_mounted_for_apt_is_binary(tmp_path):
    step = next(s for s in yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
                ["jobs"]["verify"]["steps"] if s.get("name") == STEP)
    keyring = step["env"]["KEYRING"]
    assert keyring.endswith(".gpg"), "an armored key would need a .asc name"

    script, _, docker = step["run"].partition("docker run")
    mounts = dict(m.group(2, 1) for m in re.finditer(r'--volume "([^":]+):([^":]+)(?::ro)?"', docker))
    assert keyring in mounts, f"{keyring} is not mounted: {mounts}"

    work = tmp_path / "work"
    work.mkdir()
    shutil.copy(ROOT / "public.gpg", work / "public.gpg")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "RUNNER_TEMP": str(runner_temp)}
    subprocess.run(["bash", "-c", script], cwd=work, env=env, check=True)

    host = mounts[keyring].replace("$PWD", str(work)).replace("$RUNNER_TEMP", str(runner_temp))
    data = Path(host).read_bytes()
    assert data, f"{host} is empty"
    assert not data.startswith(b"-----BEGIN PGP"), (
        f"{keyring} is ASCII-armored; apt ignores it and the InRelease check fails")
