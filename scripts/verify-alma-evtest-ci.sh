#!/usr/bin/env bash
# Real CI smoke proof. The ephemeral signing key cannot authorize publication.
set -eEuo pipefail
: "${TARGET:?}" "${GITHUB_SHA:?}" "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}"
case "$TARGET" in
  alma10) repository=https://repo.almalinux.org/almalinux/10 ;;
  alma10-kitten) repository=https://kitten.repo.almalinux.org/10-kitten ;;
  *) exit 2 ;;
esac
out="$PWD/.factory/ci-cpu-${TARGET}"
image=$(python3 - "$TARGET" <<'PY'
import sys, yaml
sys.path.insert(0, 'scripts')
from target_platform import build_context
factory = yaml.safe_load(open('manifests/package-factory.yaml'))
print(build_context(factory['targets'][sys.argv[1]], 'x86_64')['image'])
PY
)
docker pull "$image"
export CELL_ID="ci-cpu-${TARGET}" ENGINE=tideforge ARCHITECTURE=x86_64
export IMAGE="$image" FORMAT=rpm RECIPE=packages/evtest/package.yaml OUT_DIR="$out"
SOURCE_DATE_EPOCH=$(git log -1 --format=%at -- packages/evtest)
export SOURCE_DATE_EPOCH
bash scripts/run-package-factory-cell.sh
mapfile -t artifacts < <(find "$out/artifacts" -maxdepth 1 -name 'evtest-*.rpm' -type f)
[[ ${#artifacts[@]} == 1 ]] || { echo 'Expected exactly one evtest binary RPM' >&2; exit 1; }
state_parent=$(mktemp -d "${RUNNER_TEMP:?}/alma-cpu-candidate.XXXXXXXX")
state="$state_parent/state"
repo="$out/candidate"
container=""
cleanup() {
  local cleanup_status=$?
  # The collector needs root to read the measured consumer RPM database.
  # Its signature databases hold public keys only; let CI retain this proof
  # on either success or failure. Private candidate keys stay outside out.
  if [[ -d "$out/metadata/cpu-proof" ]]; then
    if ! sudo chown -R "$(id -u):$(id -g)" "$out/metadata/cpu-proof"; then
      cleanup_status=1
    fi
  fi
  if [[ -n $container ]]; then docker rm -f "$container" >/dev/null || true; fi
  gpgconf --homedir "$state/gnupg" --kill gpg-agent || true
  rm -rf -- "$state_parent"
  exit "$cleanup_status"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
python3 scripts/candidate-rpm-repository.py init --state "$state" --repo "$repo"
python3 scripts/candidate-rpm-repository.py install --state "$state" --repo "$repo" --rpm "${artifacts[0]}"
python3 scripts/candidate-rpm-repository.py index --state "$state" --repo "$repo"
fingerprint=$(python3 - "$state/identity.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))['fingerprint'])
PY
)
mkdir -p "$out/metadata/signed-candidate"
cp "$state/keys/candidate-public.gpg" "$out/metadata/candidate-public.gpg"
cp "$state/identity.json" "$out/metadata/candidate-scope.json"
mapfile -t signed < <(find "$repo" -maxdepth 1 -name 'evtest-*.rpm' -type f)
[[ ${#signed[@]} == 1 ]] || exit 1
signed_artifact="$out/metadata/signed-candidate/$(basename "${signed[0]}").input"
cp "${signed[0]}" "$signed_artifact"
container=$(docker create --volume "$repo:/candidate:ro" --volume "$state/keys:/keys:ro" \
  "$image" bash -lc '
    set -euo pipefail
    cat > /etc/yum.repos.d/tunaos-ci-candidate.repo <<REPO
[tunaos-ci-candidate]
name=Unpromoted CI candidate
baseurl=file:///candidate
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///keys/candidate-public.gpg
skip_if_unavailable=False
REPO
    dnf -y --setopt=gpgcheck=1 install evtest
    rpm -q evtest
    evtest --version
  ')
docker start --attach "$container"
docker export --output "$out/consumer.tar" "$container"
# Extract only regular files/directories/hardlinks from the trusted measured
# consumer. Ignore symlinks and devices; data filtering bounds link destinations.
python3 - "$out" <<'PY'
from pathlib import Path
import sys, tarfile
out = Path(sys.argv[1])
root = out / 'consumer-root'
root.mkdir()
with tarfile.open(out / 'consumer.tar') as archive:
    selected = (member for member in archive if member.name.split('/')[0] in {'usr', 'etc'}
                and (member.isfile() or member.isdir() or member.islnk()))
    archive.extractall(root, members=selected, filter='data')
(out / 'consumer.tar').unlink()
PY
sudo "$(command -v python3)" scripts/collect-alma-cpu-evidence.py \
  --artifact "$signed_artifact" --unsigned-artifact "${artifacts[0]}" \
  --compiler-observation "$out/artifacts/buildroots/alma/compiler/effective-flags.json" \
  --consumer-root "$out/consumer-root" --consumer-base-reference "$image" \
  --source-revision "$GITHUB_SHA" --attempt-identity "${GITHUB_RUN_ID}:${GITHUB_RUN_ATTEMPT}:${TARGET}" \
  --baseline x86-64-v2 --repository "$repository/BaseOS/x86_64_v2/os/" \
  --repository "$repository/AppStream/x86_64_v2/os/" \
  --candidate-key "$state/keys/candidate-public.gpg" --candidate-fingerprint "$fingerprint" \
  --scope candidate --output "$out/metadata/cpu-proof"
