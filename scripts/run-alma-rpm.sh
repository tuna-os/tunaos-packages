#!/usr/bin/env bash
# Runs inside the exact Alma child selected by the factory planner.
set -eEuo pipefail
: "${TARGET:?}" "${ARCHITECTURE:?}" "${BUILD_IMAGE:?}"
case "$TARGET" in alma10|alma10-kitten) ;; *) exit 2 ;; esac
case "$ARCHITECTURE" in x86_64) suffix= ;; aarch64) suffix=-aarch64 ;; *) exit 2 ;; esac
[[ $(uname -m) == "$ARCHITECTURE" ]] || { echo 'native Alma architecture mismatch' >&2; exit 2; }
[[ "$BUILD_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] || exit 2
mkdir -p /work/evidence
exec > >(tee /work/evidence/build.log) 2>&1
# Bootstrap Python using the vendor image's signed native repositories.
dnf -y --setopt=gpgcheck=1 install python3
python3 - "$TARGET" "$suffix" <<'PY'
import configparser
import pathlib
import sys
namespace = {"config_opts": {}}
path = pathlib.Path("/factory/mock") / (sys.argv[1] + "-ci" + sys.argv[2] + ".cfg")
exec(compile(path.read_bytes(), str(path), "exec"), namespace)
repos = configparser.ConfigParser(interpolation=None)
repos.read_string(namespace["config_opts"]["dnf.conf"])
for name in list(repos.sections()):
    if name not in {"main", "baseos", "appstream", "crb", "alma-epel-v2", "epel-arm"}:
        repos.remove_section(name)
repos["main"]["reposdir"] = "/etc/tunaos-build-repos"
repos["main"]["keepcache"] = "1"
root = pathlib.Path("/etc/tunaos-build-repos")
root.mkdir()
with (root / "native.repo").open("w") as stream:
    repos.write(stream)
with pathlib.Path("/etc/dnf/dnf.conf").open("w") as stream:
    stream.write("[main]\nreposdir=/etc/tunaos-build-repos\ngpgcheck=1\nkeepcache=1\n")
PY
# Cross-cell supply must be staged from an authenticated immutable snapshot.
# An arbitrary served URL cannot enter this buildroot.
if [[ -n ${PUBLISHED_INDEX:-} ]]; then
  echo 'Alma build requires a verified immutable dependency snapshot, not PUBLISHED_INDEX' >&2
  exit 1
fi
if [[ -n ${TUNAOS_CANDIDATE_REPO:-} ]]; then
  [[ $TUNAOS_CANDIDATE_REPO == /candidate-repo && -d /candidate-repo && ! -L /candidate-repo ]] || exit 2
  # The trusted host adapter authenticates the source/cell/key before mounting.
  # A mounted directory still cannot supply symlinks or secret signing material.
  python3 - <<'PY'
from pathlib import Path
p=Path('/candidate-repo')
for f in p.rglob('*'):
    if f.is_symlink() or (not f.is_file() and not f.is_dir()):
        raise SystemExit('unsafe candidate repository entry')
    if f.is_file() and not (f.name in {'candidate-public.gpg','repo.lock','admission-receipt.json'} or f.suffix == '.rpm' or f.relative_to(p).parts[0] in {'repodata','buildroots'}):
        raise SystemExit('unexpected candidate repository material')
    if f.is_file():
        with f.open('rb') as stream:
            if b'PRIVATE KEY' in stream.read(4096):
                raise SystemExit('private candidate key forbidden')
for name in ('candidate-public.gpg','repodata/repomd.xml','repodata/repomd.xml.asc'):
    if not (p/name).is_file() or not (p/name).stat().st_size:
        raise SystemExit('missing signed candidate repository')
PY
  rpm --import /candidate-repo/candidate-public.gpg
  cat > /etc/tunaos-build-repos/candidate.repo <<'EOF'
[tunaos-chain-candidate]
name=Exact run-scoped candidate
baseurl=file:///candidate-repo
gpgkey=file:///candidate-repo/candidate-public.gpg
gpgcheck=1
repo_gpgcheck=1
enabled=1
EOF
fi
dnf -y --setopt=gpgcheck=1 install rpm-build redhat-rpm-config dnf-plugins-core
dnf -y --setopt=gpgcheck=1 builddep /work/rpmbuild/SPECS/*.spec
rpm --eval '%{_arch} %{_target_cpu} %{optflags}' > /work/evidence/rpm-macros.txt
rpm --showrc > /work/evidence/rpm-showrc.txt
rpm -qa --qf '%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SIGPGP:pgpsig}\n' \
  | sort > /work/evidence/installed-buildroot.tsv
dnf repolist -v > /work/evidence/repositories.txt
dnf history info > /work/evidence/dependency-transaction.txt
for compiler in gcc clang rustc cargo go; do
  if command -v "$compiler" >/dev/null; then
    if [[ "$compiler" == go ]]; then
      "$compiler" version > "/work/evidence/${compiler}-version.txt" 2>&1
    else
      "$compiler" --version > "/work/evidence/${compiler}-version.txt" 2>&1
    fi
  fi
done
export TUNAOS_COMPILER_EVIDENCE_DIR=/work/evidence/compiler
python3 /factory/scripts/alma-rpmbuild-guard.py "$ARCHITECTURE" -ba --define '_topdir /work/rpmbuild' \
  --define "_target_cpu $ARCHITECTURE" /work/rpmbuild/SPECS/*.spec
python3 - <<'PY'
import hashlib
import json
import os
import pathlib
root = pathlib.Path('/work')
records = []
for directory in (root / 'rpmbuild/SOURCES', root / 'rpmbuild/SPECS', root / 'evidence'):
    for path in sorted(directory.rglob('*')):
        if path.is_file() and path.name not in {'build.log', 'build-inputs.json'}:
            records.append({'path': str(path.relative_to(root)), 'digest': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()})
downloads = []
for path in sorted(pathlib.Path('/var/cache/dnf').rglob('*.rpm')):
    downloads.append({'path': str(path), 'digest': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()})
document = {'schemaVersion': 1, 'kind': 'alma-build-observation',
            'target': os.environ['TARGET'], 'architecture': os.environ['ARCHITECTURE'],
            'buildImage': os.environ['BUILD_IMAGE'], 'inputs': records,
            'downloadedDependencies': downloads, 'readiness': False,
            'requiredVerification': ['authenticated-producer', 'signed-dependency-snapshot', 'artifact-cpu-inspection', 'restricted-cpu-execution']}
(root / 'evidence/build-inputs.json').write_text(json.dumps(document, sort_keys=True, indent=2) + '\n')
PY
