#!/usr/bin/env bash

# Native rpmbuild backend for build-chain.sh.
#
# Contract supplied by the orchestrator:
#   globals: FORCE, DIST, LOCAL_REPO, REPO_ROOT, RPM_SOURCES_CACHE (optional)
#   functions: find_spec, log, err
build_package_native() {
    local pkg_dir="$1"
    local spec_override="$2"

    local spec pkg_name abs_pkg_dir
    spec="$(find_spec "$pkg_dir" "$spec_override")"
    pkg_name="$(basename "$spec" .spec)"
    abs_pkg_dir="${REPO_ROOT}/${pkg_dir}"

    if ! $FORCE; then
        local nvr
        nvr=$(rpm -q --specfile "$spec" \
            --define "dist ${DIST}" \
            --queryformat "%{NAME}-%{VERSION}-%{RELEASE}\n" 2>/dev/null | head -1)
        if [[ -n "$nvr" ]] && ls "${LOCAL_REPO}/${nvr}"*.rpm &>/dev/null 2>&1; then
            log "[${pkg_name}] Skipping: ${nvr} already in local repo"
            return 0
        fi
    fi

    log "[${pkg_name}] Building (native rpmbuild) from ${pkg_dir}"

    local builddir
    builddir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '${builddir}'" RETURN

    mkdir -p "${builddir}"/{BUILD,BUILDROOT,RPMS,SOURCES,SRPMS,SPECS}

    local spec_basename
    spec_basename="$(basename "$spec")"
    cp "$spec" "${builddir}/SPECS/"

    find "$abs_pkg_dir" -maxdepth 1 -type f \
        ! -name "*.spec" \
        ! -name "sources" \
        ! -name "changelog" \
        ! -name "rpminspect.yaml" \
        ! -name "*.md" \
        -exec cp {} "${builddir}/SOURCES/" \;

    log "[${pkg_name}] Downloading sources..."
    local sources_cache="${RPM_SOURCES_CACHE:-}"
    local spectool_dest="${builddir}/SOURCES/"
    if [[ -n "$sources_cache" ]]; then
        mkdir -p "$sources_cache"
        spectool_dest="$sources_cache"
    fi

    local spectool_attempts="${SPECTOOL_ATTEMPTS:-4}"
    local spectool_delay="${SPECTOOL_DELAY:-5}"
    local spectool_log="${builddir}/spectool.log"
    local spectool_rc=0
    local attempt

    for ((attempt = 1; attempt <= spectool_attempts; attempt++)); do
        set +e
        spectool -g -C "$spectool_dest" "${builddir}/SPECS/${spec_basename}" > "$spectool_log" 2>&1
        spectool_rc=$?
        set -e

        if [[ $spectool_rc -eq 0 ]]; then
            if [[ -s "$spectool_log" ]]; then
                cat "$spectool_log"
            fi
            if [[ $attempt -gt 1 ]]; then
                log "[${pkg_name}] spectool download succeeded on attempt ${attempt}"
            fi
            break
        fi

        if [[ -s "$spectool_log" ]]; then
            cat "$spectool_log" >&2
        fi

        if grep -Eqi "(404 (Client Error|Not Found)|HTTP.*404|404:\ Not Found|status.*404|error:? 404|[Nn]ot [Ff]ound for url)" "$spectool_log"; then
            err "spectool failed for ${pkg_name}: source not found (HTTP 404); spec URL needs updating"
            return 1
        elif grep -Eqi "(name or service not known|could not resolve host|nodename nor servname|nameresolutionerror|getaddrinfo failed|cannot resolve host|unknown host|failed to resolve)" "$spectool_log"; then
            err "spectool failed for ${pkg_name}: host name does not resolve; spec URL needs updating"
            return 1
        fi

        if [[ $attempt -lt $spectool_attempts ]]; then
            log "[${pkg_name}] spectool download failed with connection error (attempt ${attempt}/${spectool_attempts}); retrying in ${spectool_delay}s..."
            sleep "$spectool_delay"
            spectool_delay=$((spectool_delay * 2))
        else
            err "spectool failed for ${pkg_name} after ${spectool_attempts} attempts (connection error)"
            return 1
        fi
    done

    if [[ -n "$sources_cache" ]]; then
        find "$sources_cache" -maxdepth 1 -type f \
            -exec ln -f {} "${builddir}/SOURCES/" \; 2>/dev/null \
            || cp "$sources_cache"/* "${builddir}/SOURCES/" 2>/dev/null || true
    fi

    log "[${pkg_name}] Installing BuildRequires..."
    dnf builddep -y \
        --define "dist ${DIST}" \
        "${builddir}/SPECS/${spec_basename}" || {
        err "dnf builddep failed for ${pkg_name}"
        return 1
    }

    log "[${pkg_name}] Running rpmbuild..."
    rpmbuild -bb \
        --define "_topdir ${builddir}" \
        --define "dist ${DIST}" \
        "${builddir}/SPECS/${spec_basename}" || {
        err "rpmbuild failed for ${pkg_name}"
        return 1
    }

    local rpm_count=0
    while IFS= read -r -d '' rpm; do
        cp "$rpm" "${LOCAL_REPO}/"
        log "[${pkg_name}] -> $(basename "$rpm")"
        rpm_count=$(( rpm_count + 1 ))
    done < <(find "${builddir}/RPMS" -name "*.rpm" -print0)

    if [[ $rpm_count -eq 0 ]]; then
        err "No RPMs produced for ${pkg_name}"
        return 1
    fi

    log "[${pkg_name}] Built ${rpm_count} RPM(s)"
}
