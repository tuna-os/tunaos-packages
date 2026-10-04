"""Exercise the publisher against repo-add's real symlink alias shape."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_signed_aliases_are_real_files_and_sources_survive(tmp_path):
    staged = tmp_path / "staged"
    repo = tmp_path / "repo"
    tools = tmp_path / "tools"
    staged.mkdir()
    tools.mkdir()
    (staged / "roost-0.1.0-1-x86_64.pkg.tar.zst").write_bytes(b"package")
    add = tools / "repo-add"
    add.write_text("""#!/bin/bash
set -eu
for arg; do case "$arg" in *.db.tar.gz) db="$arg";; esac; done
base="${db%.db.tar.gz}"
for ext in db files; do
  printf '%s' "$ext payload" > "$base.$ext.tar.gz"
  printf '%s' "$ext signature" > "$base.$ext.tar.gz.sig"
  ln -s "$(basename "$base.$ext.tar.gz")" "$base.$ext"
  ln -s "$(basename "$base.$ext.tar.gz.sig")" "$base.$ext.sig"
done
""")
    gpg = tools / "gpg"
    gpg.write_text("""#!/bin/bash
set -eu
for arg; do last="$arg"; done
printf signature > "$last.sig"
""")
    add.chmod(0o755)
    gpg.chmod(0o755)
    env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"])
    subprocess.run(["bash", str(ROOT / "scripts/publish-arch-wave.sh"),
                    "--staged", str(staged), "--repo", str(repo),
                    "--name", "tunaos", "--key", "fixture-key"],
                   env=env, check=True, capture_output=True, text=True)
    for ext in ["db", "files"]:
        for suffix in ["", ".sig"]:
            alias = repo / ("tunaos." + ext + suffix)
            source = repo / ("tunaos." + ext + ".tar.gz" + suffix)
            assert alias.is_file() and not alias.is_symlink()
            assert alias.read_bytes() == source.read_bytes()
    assert (repo / "roost-0.1.0-1-x86_64.pkg.tar.zst.sig").is_file()
