#!/usr/bin/env bats

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)"
RUNNER="${REPO_ROOT}/scripts/run-package-factory-cell.sh"

setup() {
  TEST_ROOT="$(mktemp -d)"
  mkdir -p "${TEST_ROOT}/bin"

  cat > "${TEST_ROOT}/bin/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  scripts/tideforge.py)
    if [[ ${2:-} == render ]]; then
      while (($#)); do
        if [[ $1 == --output ]]; then
          mkdir -p "$2"
          break
        fi
        shift
      done
    fi
    ;;
  scripts/assemble-deb-source-tree.py)
    mkdir -p "$3"
    ;;
esac
EOF
  chmod +x "${TEST_ROOT}/bin/python3"

  cat > "${TEST_ROOT}/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

for arg in "$@"; do
  if [[ $arg == *:/work ]]; then
    root=${arg%:/work}
    mkdir -p "$root/artifacts"
    touch "$root/artifacts/fake.deb"
    printf 'volume=%s\n' "$arg"
  fi
done
EOF
  chmod +x "${TEST_ROOT}/bin/docker"
}

teardown() {
  rm -rf "${TEST_ROOT}"
}

@test "run-package-factory-cell.sh: makes relative output directory absolute before Docker mount" {
  run bash -c '
    cd "$1"
    exec env \
      PATH="$2" \
      CELL_ID=wayland-protocols-debian-amd64 \
      ENGINE=tideforge \
      TARGET=debian \
      ARCHITECTURE=amd64 \
      IMAGE=debian:sid \
      OUT_DIR="relative output" \
      SOURCE_DATE_EPOCH=1 \
      RECIPE=packages/wayland-protocols/package.yaml \
      FORMAT=deb \
      "$3"
  ' _ "${TEST_ROOT}" "${TEST_ROOT}/bin:${PATH}" "${RUNNER}"

  [ "$status" -eq 0 ]
  [[ "$output" == *"volume=${TEST_ROOT}/relative output/deb:/work"* ]]
}
