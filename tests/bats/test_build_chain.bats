#!/usr/bin/env bats
# BATS tests for scripts/build-chain.sh — RPM Build Chain Engine
#
# build-chain.sh requires a build-order.yml, RPM build tools, and either
# podman or mock. These tests validate structure, argument parsing, and
# error handling without running an actual build.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)"
BUILD_CHAIN="${REPO_ROOT}/scripts/build-chain.sh"

@test "build-chain.sh: exists and is executable" {
  run test -x "${BUILD_CHAIN}"
  [ "$status" -eq 0 ]
}

@test "build-chain.sh: has bash shebang" {
  run head -1 "${BUILD_CHAIN}"
  [[ "$output" =~ ^#!/.*bash ]]
}

@test "build-chain.sh: shows usage with --help" {
  run bash "${BUILD_CHAIN}" --help
  [ "$status" -eq 0 ]
  [[ "$output" =~ Usage ]]
}

@test "build-chain.sh: shows usage with --manifest without file" {
  run bash "${BUILD_CHAIN}" --manifest
  [ "$status" -ne 0 ]
}

@test "build-chain.sh: references build-order.yml in default operation" {
  run bash "${BUILD_CHAIN}" --backend podman --dry-run 2>&1 || true
  # Should mention build-order.yml or fail gracefully about missing tools
  [[ "$output" =~ build-order ]] || [[ "$output" =~ manifest ]]
}

@test "build-chain.sh: passes shellcheck" {
  if command -v shellcheck &>/dev/null; then
    run shellcheck "${BUILD_CHAIN}" "${REPO_ROOT}/scripts/lib/build-chain/native.sh"
    [ "$status" -eq 0 ]
  else
    skip "shellcheck not installed"
  fi
}

@test "build-chain.sh: loads native backend through the module boundary" {
  run grep -F 'source "${SCRIPT_DIR}/lib/build-chain/native.sh"' "${BUILD_CHAIN}"
  [ "$status" -eq 0 ]

  run grep -F 'build_package_native()' "${BUILD_CHAIN}"
  [ "$status" -ne 0 ]

  run grep -F 'build_package_native()' "${REPO_ROOT}/scripts/lib/build-chain/native.sh"
  [ "$status" -eq 0 ]
}

@test "build-chain.sh: _record_result is a no-op when BUILD_RESULTS_FILE is unset" {
  # Truncated copy (drop the trailing `main "$@"` call) so sourcing it just
  # defines functions/vars instead of running a build.
  local tmp_script="${REPO_ROOT}/scripts/.test-build-chain-$$.sh"
  head -n -1 "${BUILD_CHAIN}" > "${tmp_script}"

  run bash -c "cd '${REPO_ROOT}' && unset BUILD_RESULTS_FILE; set --; source '${tmp_script}'; _record_result pkg tier success 1 false; echo ok"
  rm -f "${tmp_script}"
  [ "$status" -eq 0 ]
  [[ "$output" =~ ok ]]
}

@test "build-chain.sh: _record_result writes one JSONL object per call when BUILD_RESULTS_FILE is set" {
  local tmp_script="${REPO_ROOT}/scripts/.test-build-chain-$$.sh"
  head -n -1 "${BUILD_CHAIN}" > "${tmp_script}"
  local results_file
  results_file="$(mktemp)"

  run bash -c "cd '${REPO_ROOT}' && export BUILD_RESULTS_FILE='${results_file}'; set --; source '${tmp_script}'; _record_result src/foo tierA success 12 false; _record_result src/bar tierA failure 3 true"
  rm -f "${tmp_script}"
  [ "$status" -eq 0 ]

  [ "$(wc -l < "${results_file}")" -eq 2 ]
  run python3 -c "import json,sys; [json.loads(l) for l in open('${results_file}')]"
  [ "$status" -eq 0 ]

  run grep -c '"outcome": "failure"' "${results_file}"
  [ "$output" -eq 1 ]
  rm -f "${results_file}"
}
