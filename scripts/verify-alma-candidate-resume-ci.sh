#!/usr/bin/env bash
# Two dispatches prove candidate resume; neither can authorize publication.
set -eEuo pipefail
: "${PHASE:?}" "${RUNNER_TEMP:?}" "${GITHUB_SHA:?}"
[[ "$PHASE" == produce || "$PHASE" == resume ]] || exit 2
out="$PWD/.factory/ci-alma-resume"
meta="$out/candidate-meta"
repo="$out/candidate"
mkdir -p "$out"
python3 - "$out/cell.json" <<'PY'
import json, pathlib, yaml, sys
factory = yaml.safe_load(pathlib.Path('manifests/package-factory.yaml').read_text())
target = factory['targets']['alma10']
cell = {'id': 'ci-alma-resume', 'target': 'alma10', 'platform': target['platforms']['x86_64'],
        'cpu_baseline': target['cpu_baselines']['x86_64'], 'verify_image': target['probe_images']['x86_64'],
        'signer_workflow': '.github/workflows/alma-candidate-resume-ci.yml'}
pathlib.Path(sys.argv[1]).write_text(json.dumps(cell, sort_keys=True))
PY
image=$(jq -r .verify_image "$out/cell.json")
action_key="sha256:$(sha256sum "$out/cell.json" | cut -d' ' -f1)"
python3 scripts/alma-candidate-resume.py prepare --meta "$meta" --cell "$out/cell.json" --action-key "$action_key"
state_parent=$(mktemp -d "$RUNNER_TEMP/alma-resume-private.XXXXXXXX")
state="$state_parent/state"
cleanup() {
  gpgconf --homedir "$state/gnupg" --kill gpg-agent || true
  rm -rf -- "$state_parent"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
python3 scripts/candidate-rpm-repository.py init --state "$state" --repo "$repo"
cp "$state/keys/candidate-public.gpg" "$meta/candidate-public.gpg"
cp "$state/identity.json" "$meta/candidate-identity.json"
docker pull "$image"
export SOURCE_DATE_EPOCH
SOURCE_DATE_EPOCH=$(git log -1 --format=%at -- packages/evtest)
prepared="$out/rpm/rpmbuild"
if [[ "$PHASE" == produce ]]; then
  export CELL_ID=ci-alma-resume ENGINE=tideforge TARGET=alma10 ARCHITECTURE=x86_64
  export IMAGE="$image" FORMAT=rpm RECIPE=packages/evtest/package.yaml OUT_DIR="$out"
  bash scripts/run-package-factory-cell.sh
  mapfile -t rpms < <(find "$out/artifacts" -maxdepth 1 -name 'evtest-*.rpm' -type f)
  [[ ${#rpms[@]} == 1 ]] || exit 1
  python3 scripts/candidate-rpm-repository.py install --state "$state" --repo "$repo" --rpm "${rpms[0]}"
  python3 scripts/candidate-rpm-repository.py index --state "$state" --repo "$repo"
  python3 scripts/alma-candidate-resume.py record --meta "$meta" --repo "$repo" \
    --name evtest --builddir "$prepared" --output "$(basename "${rpms[0]}")"
  if [[ -d "$out/artifacts/buildroots" ]]; then
    cp -R "$out/artifacts/buildroots" "$repo/buildroots"
  fi
  python3 scripts/alma-candidate-resume.py create --meta "$meta" --repo "$repo" \
    --destination "$out/native-snapshot"
else
  python3 scripts/alma-candidate-resume.py restore --meta "$meta" --destination "$out/candidate-resume"
  test -s "$out/candidate-resume/snapshot.json"
  python3 scripts/alma-candidate-resume.py admit --meta "$meta" --repo "$repo" \
    --state "$state" --destination "$out/candidate-resume"
  # Recreate actual package inputs without building the RPM again.
  mkdir -p "$prepared/SPECS" "$prepared/SOURCES"
  python3 scripts/tideforge.py render packages/evtest/package.yaml --target alma10 --output "$prepared/SPECS"
  python3 scripts/fetch-tideforge-sources.py packages/evtest/package.yaml "$prepared/SOURCES" \
    --cache-dir "$HOME/.cache/tideforge/sources"
  python3 scripts/alma-candidate-resume.py skip --meta "$meta" --repo "$repo" --name evtest --builddir "$prepared"
  source_file=$(find "$prepared/SOURCES" -type f -print -quit)
  test -n "$source_file"
  cp "$source_file" "$out/original-source"
  printf 'changed input\n' >> "$source_file"
  if python3 scripts/alma-candidate-resume.py skip --meta "$meta" --repo "$repo" --name evtest --builddir "$prepared"; then
    echo 'Changed package input incorrectly skipped' >&2; exit 1
  fi
  cp "$out/original-source" "$source_file"
  python3 - "$repo/admission-receipt.json" <<'PY'
import json, os, sys
receipt = json.load(open(sys.argv[1]))
assert receipt['productionReady'] is False
assert receipt['producer']['sourceRevision'] == os.environ['GITHUB_SHA']
assert receipt['producer']['runId'] != int(os.environ['GITHUB_RUN_ID'])
assert receipt['rpms'] and all(row['oldDigest'] != row['newDigest'] for row in receipt['rpms'])
assert all(row['payloadDigest'].startswith('sha256:') for row in receipt['rpms'])
PY
fi
# Use the actual native child and require both RPM and repository signatures.
docker run --rm --volume "$repo:/candidate:ro" --volume "$state/keys:/keys:ro" "$image" bash -lc '
  set -euo pipefail
  rpm --import /keys/candidate-public.gpg
  cat > /etc/yum.repos.d/alma-resume-ci.repo <<REPO
[alma-resume-ci]
name=Unpromoted authenticated CI candidate
baseurl=file:///candidate
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///keys/candidate-public.gpg
skip_if_unavailable=0
priority=1
REPO
  dnf -y --setopt=gpgcheck=1 --setopt=localpkg_gpgcheck=1 install evtest
  rpm -q evtest
  evtest --version
'
