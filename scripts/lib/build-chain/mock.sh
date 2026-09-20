#!/usr/bin/env bash

# Host-Mock backend for build-chain.sh.
#
# Contract supplied by the orchestrator:
#   globals: FORCE, REPO_ROOT, BUILD_IMAGE, DIST, SRPM_BCOND_ARGS,
#            LOCAL_REPO, WITH_CHECKS, MOCK_CONFIG, MOCK_BCOND_ARGS
#   functions: find_spec, check_package_exists, prepare_sources,
#              record_buildroot_manifest
build_package_mock() {
    local pkg_dir="$1"
    local spec_override="$2"

    local spec pkg_name abs_pkg_dir builddir
    spec="$(find_spec "$pkg_dir" "$spec_override")"
    pkg_name="$(basename "$spec" .spec)"
    abs_pkg_dir="${REPO_ROOT}/${pkg_dir}"

    if ! $FORCE && check_package_exists "$pkg_name" "$spec"; then
        return 0
    fi

    echo "==> [${pkg_name}] Building (mock) from ${pkg_dir}"

    builddir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '${builddir}'" RETURN

    prepare_sources "$builddir" "$spec" "$abs_pkg_dir"

    # Build SRPM inside the container (to ensure macros like %autorelease are available)
    local spec_basename
    spec_basename="$(basename "$spec")"
    echo "==> [${pkg_name}] Building SRPM..."
    podman run --rm \
        --pull=always \
        -v "${builddir}:/builddir:Z" \
        "${BUILD_IMAGE}" \
        rpmbuild -bs "/builddir/SPECS/${spec_basename}" \
            --define "_topdir /builddir" \
            --define "dist ${DIST}" \
            "${SRPM_BCOND_ARGS[@]}"

    local srpm
    srpm="$(find "${builddir}/SRPMS" -name "*.src.rpm" | head -1)"
    if [[ -z "$srpm" ]]; then
        echo "ERROR: No SRPM produced for ${pkg_name}" >&2
        return 1
    fi

    echo "==> [${pkg_name}] Rebuilding with mock (uniqueext=${pkg_name})..."
    local resultdir="${builddir}/results"
    mkdir -p "$resultdir"

    # Ensure the local repo metadata is up-to-date before mock starts,
    # locked to prevent parallel jobs from corrupting it.
    flock "${LOCAL_REPO}/repo.lock" -c "createrepo_c --update \"${LOCAL_REPO}\""

    local mock_check_flag="--nocheck"
    $WITH_CHECKS && mock_check_flag=""

    flock "${LOCAL_REPO}/repo.lock" -c "
        mock -r \"${MOCK_CONFIG}\" \\
            --uniqueext=\"${pkg_name}\" \\
            --rebuild \"$srpm\" \\
            --resultdir=\"$resultdir\" \\
            --define \"dist ${DIST}\" \\
            ${mock_check_flag} ${MOCK_BCOND_ARGS} \\
            --no-clean \\
            --no-cleanup-after || {
                echo 'ERROR: mock failed. Printing build.log:';
                cat \"$resultdir/build.log\" || true;
                echo 'ERROR: Printing root.log:';
                cat \"$resultdir/root.log\" || true;
                exit 1;
            }
        "

    local rpm_count=0
    while IFS= read -r -d '' rpm; do
        cp "$rpm" "${LOCAL_REPO}/"
        echo "==> [${pkg_name}] -> $(basename "$rpm")"
        rpm_count=$(( rpm_count + 1 ))
    done < <(find "$resultdir" -name "*.rpm" ! -name "*.src.rpm" -print0)

    record_buildroot_manifest "$resultdir" "$pkg_name"

    if [[ $rpm_count -eq 0 ]]; then
        echo "ERROR: No RPMs produced for ${pkg_name}" >&2
        return 1
    fi

    echo "==> [${pkg_name}] Built ${rpm_count} RPM(s)"
}
