#!/usr/bin/env bash
# Actual native provider queue; only signed candidate input, never publication.
set -eEuo pipefail
# Include API discovery/admission and key/bootstrap setup in the job budget.
export CHAIN_STARTED_MONOTONIC="$(python3 -c 'import time; print(time.monotonic())')"
: "${TARGET:?}" "${ARCHITECTURE:?}" "${SCOPE:?}" "${RUNNER_TEMP:?}" "${GITHUB_SHA:?}"
case "$TARGET" in alma10|alma10-kitten) ;; *) exit 2 ;; esac
case "$ARCHITECTURE" in x86_64|aarch64) ;; *) exit 2 ;; esac
case "$SCOPE" in canary|full) ;; *) exit 2 ;; esac
cell="tideforge-chain-${TARGET}-${ARCHITECTURE}"
out="$PWD/.factory/$cell"
meta="$out/candidate-meta"
repo="$out/candidate"
mkdir -p "$out"
key=$(python3 - "$TARGET" "$ARCHITECTURE" "$SCOPE" "$GITHUB_SHA" <<'PY'
import hashlib,json,sys
print('sha256:'+hashlib.sha256(json.dumps({'engine':'alma-recipe-chain-v1','target':sys.argv[1],
 'architecture':sys.argv[2],'scope':sys.argv[3],'sourceRevision':sys.argv[4]},sort_keys=True).encode()).hexdigest())
PY
)
args=()
if [[ "$SCOPE" == canary ]]; then
  args=(--package cosmic-icon-theme)
else
  mapfile -t names < <(python3 - "$TARGET" <<'PY'
from pathlib import Path
import sys,yaml
names=[]
for p in sorted(Path('packages').glob('*/package.yaml')):
 r=yaml.safe_load(p.read_text())
 if sys.argv[1] in r.get('targets',[]):names.append(r['name'])
print('\n'.join(sorted(names)))
PY
)
  ((${#names[@]} > 0))
  for name in "${names[@]}"; do args+=(--package "$name"); done
fi
python3 scripts/alma-recipe-chain.py --target "$TARGET" --architecture "$ARCHITECTURE" \
  --source-revision "$GITHUB_SHA" --action-key "$key" "${args[@]}" --output "$out/plan.json"
python3 - "$out/plan.json" "$out/cell.json" <<'PY'
import json,pathlib,sys
p=json.load(open(sys.argv[1]))
c={'id':p['cell'],'target':p['target'],'platform':p['platform'],'cpu_baseline':p['cpuBaseline'],
   'verify_image':p['image'],'signer_workflow':'.github/workflows/alma-recipe-chain-ci.yml'}
pathlib.Path(sys.argv[2]).write_text(json.dumps(c,sort_keys=True))
PY
python3 scripts/alma-candidate-resume.py prepare --meta "$meta" --cell "$out/cell.json" --action-key "$key"
private_parent=$(mktemp -d "$RUNNER_TEMP/alma-chain-private.XXXXXXXX")
state="$private_parent/state"
cleanup() {
  local result=$?
  # Container output can be root-owned. Keep the complete public diagnosis.
  if ! sudo chown --no-dereference -R "$(id -u):$(id -g)" "$out"; then result=1; fi
  gpgconf --homedir "$state/gnupg" --kill gpg-agent || true
  rm -rf -- "$private_parent"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
python3 scripts/candidate-rpm-repository.py init --state "$state" --repo "$repo"
cp "$state/keys/candidate-public.gpg" "$meta/candidate-public.gpg"
cp "$state/identity.json" "$meta/candidate-identity.json"
python3 scripts/candidate-rpm-repository.py index --state "$state" --repo "$repo"
python3 scripts/alma-candidate-resume.py restore --meta "$meta" --destination "$out/candidate-resume"
if [[ -s "$out/candidate-resume/snapshot.json" ]]; then
  python3 scripts/alma-candidate-resume.py admit --meta "$meta" --repo "$repo" \
    --state "$state" --destination "$out/candidate-resume"
fi
status=0
python3 scripts/run-alma-recipe-chain.py --plan "$out/plan.json" --repo "$repo" \
  --state "$state" --meta "$meta" --work "$out/work" --output "$out/result.json" \
  --engine docker || status=$?
# Bank only genuinely completed signed packages, including a failed later wave.
if [[ -s "$meta/completed-packages.json" ]] && jq -e 'length > 0' "$meta/completed-packages.json" >/dev/null; then
  complete=()
  if [[ "$status" == 0 ]] && jq -e '.chainComplete == true' "$out/result.json" >/dev/null; then complete=(--chain-complete); fi
  python3 scripts/alma-candidate-resume.py create --meta "$meta" --repo "$repo" \
    --destination "$out/native-snapshot" "${complete[@]}"
  if [[ -n ${GITHUB_OUTPUT:-} ]]; then echo 'created=true' >> "$GITHUB_OUTPUT"; fi
fi
exit "$status"
