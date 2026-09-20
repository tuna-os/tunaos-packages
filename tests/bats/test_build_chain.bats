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
    run shellcheck "${BUILD_CHAIN}" \
      "${REPO_ROOT}/scripts/lib/build-chain/native.sh" \
      "${REPO_ROOT}/scripts/lib/build-chain/mock.sh"
    [ "$status" -eq 0 ]
  else
    skip "shellcheck not installed"
  fi
}

@test "build-chain.sh: loads mock backend through the module boundary" {
  run grep -F 'source "${SCRIPT_DIR}/lib/build-chain/mock.sh"' "${BUILD_CHAIN}"
  [ "$status" -eq 0 ]

  run grep -F 'build_package_mock()' "${BUILD_CHAIN}"
  [ "$status" -ne 0 ]

  run grep -F 'build_package_mock()' "${REPO_ROOT}/scripts/lib/build-chain/mock.sh"
  [ "$status" -eq 0 ]
}

@test "build-chain.sh: loads native backend through the module boundary" {
  run grep -F 'source "${SCRIPT_DIR}/lib/build-chain/native.sh"' "${BUILD_CHAIN}"
  [ "$status" -eq 0 ]

  run grep -F 'build_package_native()' "${BUILD_CHAIN}"
  [ "$status" -ne 0 ]

  run grep -F 'build_package_native()' "${REPO_ROOT}/scripts/lib/build-chain/native.sh"
  [ "$status" -eq 0 ]
}
