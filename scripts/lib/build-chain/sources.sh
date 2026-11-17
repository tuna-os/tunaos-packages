#!/usr/bin/env bash

# Spec and source staging service for build-chain.sh: locate a package's spec,
# stage its spec, patches and upstream/lookaside sources into a build tree,
# and decide whether its NVR is already built locally or served.
#
# Contract supplied by the orchestrator:
#   globals: ALMA_CANDIDATE, ALMA_CANDIDATE_META, BUILD_IMAGE, DIST,
#            LOCAL_REPO, REPO_ROOT, RPM_SOURCES_CACHE, SCRIPT_DIR,
#            SERVED_NVRS_SET, SPECTOOL_ATTEMPTS, SPECTOOL_DELAY, WITH_CHECKS
#   functions: err

find_spec() {
    local pkg_dir="$1"
    local spec_override="$2"

    if [[ -n "$spec_override" ]]; then
        local spec="${REPO_ROOT}/${pkg_dir}/${spec_override}"
        if [[ -f "$spec" ]]; then
            echo "$spec"
            return
        fi
        err "spec_override '${spec_override}' not found in ${pkg_dir}"
        return 1
    fi

    local dir_name
    dir_name="$(basename "$pkg_dir")"
    local default_spec="${REPO_ROOT}/${pkg_dir}/${dir_name}.spec"
    if [[ -f "$default_spec" ]]; then
        echo "$default_spec"
        return
    fi

    # Fallback: any .spec that isn't a bootstrap/rawhide variant
    local specs=()
    while IFS= read -r -d '' f; do
        if [[ ! "$f" =~ -bootstrap\.spec$ ]] && [[ ! "$f" =~ -rawhide\.spec$ ]]; then
            specs+=("$f")
        fi
    done < <(find "${REPO_ROOT}/${pkg_dir}" -maxdepth 1 -name "*.spec" -print0)

    if [[ ${#specs[@]} -eq 1 ]]; then
        echo "${specs[0]}"
        return
    fi

    err "Cannot determine spec for ${pkg_dir} (found ${#specs[@]} candidates)"
    return 1
}

# Move a %find_lang out of %check and onto the end of %install.
#
# rpmbuild --nocheck does not skip the %check *command*, it skips the whole
# %check *section* -- and a spec is free to put anything after %check that is
# not a section header of its own. Fedora's iso-codes does exactly that:
#
#   %check
#   %meson_test
#
#   %find_lang %{name} --all-name
#
#   %files -f %{name}.lang
#
# so with --nocheck the .lang file is never generated and the build dies in
# %files, on a spec that is perfectly correct in Koji:
#
#   error: Could not open %files file .../iso-codes.lang: No such file or directory
#
# %find_lang only reads $RPM_BUILD_ROOT, so the end of %install is both where
# it belongs and where it behaves identically. Rewrite the staged copy, never
# the tree: an imported spec is a verbatim record of dist-git.
#
# Confined to specs that actually have the shape (checks off, a %find_lang
# inside %check, and an %install to move it to); every other spec is passed
# through byte for byte.
hoist_find_lang_out_of_check() {
    local staged_spec="$1"
    local pkg_name="$2"

    $WITH_CHECKS && return 0
    grep -q '^[[:space:]]*%find_lang' "$staged_spec" || return 0

    local rewritten="${staged_spec}.hoisted"
    awk '
        function is_section_header(l) {
            return (l ~ /^%(package|description|prep|generate_buildrequires|conf|build|install|check|files|changelog|pre|post|preun|postun|pretrans|posttrans|verifyscript|trigger|filetrigger|transfiletrigger|sepolicy|patchlist|sourcelist)([[:space:]]|$)/)
        }
        {
            line[NR] = $0
            if (is_section_header($0)) {
                if ($0 ~ /^%install([[:space:]]|$)/)     section = "install"
                else if ($0 ~ /^%check([[:space:]]|$)/)  section = "check"
                else                                     section = "other"
            }
            if (section == "install") install_end = NR
            if (section == "check" && $0 ~ /^[[:space:]]*%find_lang/) hoist[NR] = 1
        }
        END {
            hoisted = 0
            for (i = 1; i <= NR; i++) if (i in hoist) hoisted++
            if (hoisted == 0 || install_end == 0) { for (i = 1; i <= NR; i++) print line[i]; exit 0 }
            for (i = 1; i <= NR; i++) {
                if (i in hoist) continue
                print line[i]
                if (i == install_end)
                    for (j = 1; j <= NR; j++) if (j in hoist) print line[j]
            }
        }
    ' "$staged_spec" > "$rewritten" || { err "spec rewrite failed for ${pkg_name}"; return 1; }

    if ! cmp -s "$staged_spec" "$rewritten"; then
        echo "==> [${pkg_name}] %find_lang sits in %check, which --nocheck skips; hoisting it to %install"
        mv "$rewritten" "$staged_spec"
    else
        rm -f "$rewritten"
    fi
}

# Prepare a build tree (spec + patches + downloaded sources) in $builddir.
# Does NOT build — just stages everything so a backend can pick it up.
prepare_sources() {
    local builddir="$1"
    local spec="$2"
    local abs_pkg_dir="$3"
    local pkg_name
    pkg_name="$(basename "$spec" .spec)"

    mkdir -p "${builddir}"/{BUILD,BUILDROOT,RPMS,SOURCES,SRPMS,SPECS}

    cp "$spec" "${builddir}/SPECS/"
    hoist_find_lang_out_of_check "${builddir}/SPECS/$(basename "$spec")" "$pkg_name" || return 1

    # Copy patches and other sources (don't exclude tarballs/zips if they exist locally)
    find "$abs_pkg_dir" -maxdepth 1 -type f \
        ! -name "*.spec" \
        ! -name "sources" \
        ! -name "changelog" \
        ! -name "rpminspect.yaml" \
        ! -name "*.md" \
        -exec cp {} "${builddir}/SOURCES/" \;

    # Download tarballs. Run spectool inside BUILD_IMAGE (has rpmdevtools)
    # so the host doesn't need rpmdevtools — it's Fedora-only.
    #
    # $RPM_SOURCES_CACHE, when set, is a host directory that outlives the run.
    # The mock backend has honoured it for a long time; this path did not, so
    # every dispatch re-downloaded every upstream tarball -- and this is the
    # path the Hummingbird desktop builds take, where a tier is a hundred-odd
    # packages and a full desktop is over a thousand.
    #
    # Downloads land in the cache and are hard-linked into the builddir, so a
    # second package needing the same archive costs an inode rather than a
    # transfer. Falls back to copying across filesystems.
    echo "==> [${pkg_name}] Downloading sources via spectool..."
    local sources_cache="${RPM_SOURCES_CACHE:-}"
    local spectool_dest_host="${builddir}/SOURCES"
    local spectool_dest_container="/builddir/SOURCES"
    if [[ -n "$sources_cache" ]]; then
        mkdir -p "$sources_cache"
        spectool_dest_host="$sources_cache"
        spectool_dest_container="/sources-cache"
    fi
    # Retry with backoff on transient connection failures (#683). A network
    # unreachable blip or connection reset must not sink the chain leg, while
    # a 404 or unresolvable hostname indicates a spec URL that needs updating
    # and fails immediately without wasteful retries.
    _run_spectool() {
        if command -v spectool &>/dev/null; then
            spectool -g -C "${spectool_dest_host}" "$spec"
        else
            podman run --rm \
                --pull=always \
                -v "${builddir}:/builddir:Z" \
                ${sources_cache:+-v "${sources_cache}:/sources-cache:Z"} \
                "${BUILD_IMAGE}" \
                spectool -g -C "${spectool_dest_container}" "/builddir/SPECS/$(basename "$spec")"
        fi
    }

    local spectool_attempts="${SPECTOOL_ATTEMPTS:-4}"
    local spectool_delay="${SPECTOOL_DELAY:-5}"
    local spectool_log="${builddir}/spectool.log"
    local spectool_rc=0
    local attempt

    for ((attempt = 1; attempt <= spectool_attempts; attempt++)); do
        # Capture the status in an if-condition, not under `set +e`: the
        # script's ERR trap (set -E) still fires and exits under `set +e`,
        # which would abort on the first failure and never retry.
        if _run_spectool > "$spectool_log" 2>&1; then
            spectool_rc=0
        else
            spectool_rc=$?
        fi

        if [[ $spectool_rc -eq 0 ]]; then
            if [[ -s "$spectool_log" ]]; then
                cat "$spectool_log"
            fi
            if [[ $attempt -gt 1 ]]; then
                echo "==> [${pkg_name}] spectool download succeeded on attempt ${attempt}"
            fi
            break
        fi

        if [[ -s "$spectool_log" ]]; then
            cat "$spectool_log" >&2
        fi

        # Check for permanent errors (404 or name resolution failure) vs retryable connection errors.
        if grep -Eqi "(404 (Client Error|Not Found)|HTTP.*404|404:\ Not Found|status.*404|error:? 404|[Nn]ot [Ff]ound for url)" "$spectool_log"; then
            echo "ERROR: spectool failed for ${pkg_name}: source not found (HTTP 404); spec URL needs updating" >&2
            return 1
        elif grep -Eqi "(name or service not known|could not resolve host|nodename nor servname|nameresolutionerror|getaddrinfo failed|cannot resolve host|unknown host|failed to resolve)" "$spectool_log"; then
            echo "ERROR: spectool failed for ${pkg_name}: host name does not resolve; spec URL needs updating" >&2
            return 1
        fi

        if [[ $attempt -lt $spectool_attempts ]]; then
            echo "==> [${pkg_name}] spectool download failed with connection error (attempt ${attempt}/${spectool_attempts}); retrying in ${spectool_delay}s..." >&2
            sleep "$spectool_delay"
            spectool_delay=$((spectool_delay * 2))
        else
            echo "ERROR: spectool failed for ${pkg_name} after ${spectool_attempts} attempts (connection error)" >&2
            return 1
        fi
    done
    if [[ -n "$sources_cache" ]]; then
        # Never let a cached download land on top of a file that came out of
        # the package directory. Those files are what Fedora committed to
        # dist-git; the cache holds whatever a URL served at download time,
        # and for a spec that carries a patch by URL those are not the same
        # thing.
        #
        # luajit is the worked example. Its Patch2 is
        # https://github.com/luajit/luajit/pull/631.patch, a live pull request
        # whose diff is regenerated against a moving base, and dist-git also
        # ships the pinned copy as luajit-2.1-s390x-support.patch. `ln -f`
        # clobbered the pinned copy with today's regenerated one, which no
        # longer applies: "2 out of 4 hunks FAILED -- saving rejects to file
        # src/lj_ccall.c.rej", %prep dead, run 31294475023 layer-00.
        local cached
        while IFS= read -r -d '' cached; do
            [[ -e "${builddir}/SOURCES/$(basename "$cached")" ]] && continue
            ln -f "$cached" "${builddir}/SOURCES/" 2>/dev/null \
                || cp "$cached" "${builddir}/SOURCES/" 2>/dev/null || true
        done < <(find "$sources_cache" -maxdepth 1 -type f -print0)
    fi

    # Re-assert the dist-git files over anything spectool fetched. When
    # $RPM_SOURCES_CACHE is unset spectool downloads straight into SOURCES,
    # so the guard above never sees those writes; this makes "dist-git wins"
    # true on both paths rather than only on the cached one.
    find "$abs_pkg_dir" -maxdepth 1 -type f \
        ! -name "*.spec" \
        ! -name "sources" \
        ! -name "changelog" \
        ! -name "rpminspect.yaml" \
        ! -name "*.md" \
        -exec cp -f {} "${builddir}/SOURCES/" \;

    # Fetch dist-git lookaside sources. A package imported from Fedora dist-git
    # can list artifacts in its `sources` file that have no URL in the spec at
    # all (selinux-policy's container-selinux.tgz is a repacked git snapshot) —
    # spectool cannot download those, and they are not loose files in the
    # package directory. COPR's rpkg tooling fetched them from the lookaside
    # cache natively, which is why this gap only surfaced on the first full
    # GitHub-side chain build (run 30662870608, tier base-tools). Fetch any
    # sources-file entry still missing after the copy and spectool steps, and
    # verify it against the recorded checksum before trusting it.
    local sources_file="${abs_pkg_dir}/sources"
    if [[ -f "$sources_file" ]]; then
        local lookaside_name entry_name entry_hash
        lookaside_name="$(basename "$abs_pkg_dir")"
        local entry_algo
        while IFS= read -r line; do
            # Two formats, both current in Rawhide today.
            #
            #   SHA512 (foo-1.0.tar.gz) = <128 hex>     the modern one
            #   <32 hex>  foo-1.0.tar.gz                the legacy md5 one
            #
            # Matching only the first silently skips the second -- `continue`
            # on a line that is not an error -- and the package then dies in
            # rpmbuild with "Bad file: /builddir/SOURCES/...: No such file or
            # directory", which names the tarball but not the reason.
            #
            # That is what happened to lockdev and redhat-menus in gnome-00 of
            # run 31272392927. Both still carry md5 sources files; both are
            # old packages nobody has re-uploaded, and there are more like them
            # across a 1248-package manifest.
            if [[ "$line" =~ ^SHA512\ \((.+)\)\ =\ ([0-9a-f]{128})$ ]]; then
                entry_name="${BASH_REMATCH[1]}"
                entry_hash="${BASH_REMATCH[2]}"
                entry_algo="sha512"
            elif [[ "$line" =~ ^([0-9a-f]{32})\ \ (.+)$ ]]; then
                entry_hash="${BASH_REMATCH[1]}"
                entry_name="${BASH_REMATCH[2]}"
                entry_algo="md5"
            else
                continue
            fi
            # Present is not the same as correct. A `sources` entry pins an
            # exact artifact, but plenty of Rawhide specs point Source0 at a
            # moving ref -- luajit's is
            # https://github.com/LuaJIT/LuaJIT/archive/refs/heads/v2.1/...,
            # the branch head. Fedora builds from the lookaside copy the
            # checksum names; spectool downloads whatever that branch is
            # today. The two drifted, so the patches (cut against the pinned
            # snapshot) stopped applying and luajit failed %prep in
            # run 31294475023 while Fedora's own build of the same commit is
            # fine.
            #
            # So verify what is staged against the recorded hash, and treat a
            # mismatch exactly like a missing file: discard it and take the
            # lookaside copy, which is by definition the artifact the rest of
            # the packaging was written against.
            if [[ -f "${builddir}/SOURCES/${entry_name}" ]]; then
                if echo "${entry_hash}  ${builddir}/SOURCES/${entry_name}" \
                    | "${entry_algo}sum" --check --status -; then
                    continue
                fi
                echo "==> [${pkg_name}] ${entry_name} does not match the ${entry_algo} in dist-git sources; refetching"
                # rm rather than overwrite: with $RPM_SOURCES_CACHE this file
                # is a hard link to the cached copy, and writing through it
                # would edit the cache behind its own checksum check.
                rm -f "${builddir}/SOURCES/${entry_name}"
            fi
            echo "==> [${pkg_name}] Fetching ${entry_name} from the Fedora lookaside cache (${entry_algo})..."
            # The lookaside path carries the algorithm, so the legacy entries
            # live under .../md5/<hash>/... and not under sha512.
            curl -fsSL --retry 3 \
                -o "${builddir}/SOURCES/${entry_name}" \
                "https://src.fedoraproject.org/lookaside/pkgs/rpms/${lookaside_name}/${entry_name}/${entry_algo}/${entry_hash}/${entry_name}" || {
                err "lookaside fetch failed for ${entry_name} (${pkg_name})"
                return 1
            }
            echo "${entry_hash}  ${builddir}/SOURCES/${entry_name}" \
                | "${entry_algo}sum" --check --quiet - || {
                err "lookaside checksum mismatch for ${entry_name} (${pkg_name})"
                return 1
            }
            # Persist it, so the next run and the next package needing the same
            # archive get it for free. Only after the checksum has passed --
            # a cache is a much worse place to put a corrupt file than a
            # builddir that is about to be deleted.
            if [[ -n "${RPM_SOURCES_CACHE:-}" ]]; then
                ln -f "${builddir}/SOURCES/${entry_name}" \
                    "${RPM_SOURCES_CACHE}/${entry_name}" 2>/dev/null \
                    || cp "${builddir}/SOURCES/${entry_name}" \
                        "${RPM_SOURCES_CACHE}/${entry_name}" 2>/dev/null || true
            fi
        done < "$sources_file"
    fi
}

# Check if the package already exists in the local repo with the same NVR
check_package_exists() {
    # Only the run-local signature gate can accept Alma candidates; no
    # restored RPM or served NVR may suppress a required compatible rebuild.
    if $ALMA_CANDIDATE; then
        [[ -n "${ALMA_CANDIDATE_META:-}" && -n "${3:-}" ]] || return 1
        if python3 "${SCRIPT_DIR}/alma-candidate-resume.py" skip \
            --meta "$ALMA_CANDIDATE_META" --repo "$LOCAL_REPO" --name "$1" --builddir "$3"; then
            echo "==> [$1] Skipping authenticated completion with identical prepared inputs"
            return 0
        fi
        return 1
    fi
    local pkg_name="$1"
    local spec="$2"
    local spec_basename
    spec_basename="$(basename "$spec")"

    # Query the spec file inside the container to get the expected NVR
    # We do this inside the container to ensure macro expansion (%autorelease, %dist, etc.)
    #
    # --pull=missing, not --pull=always. BUILD_IMAGE is a fixed tag, and the
    # workflow already pulls it once in its own "Pull mock runner" step, so
    # --pull=always bought nothing but a GHCR round-trip -- and this function
    # runs for EVERY package the tier considers, before the skip decision, so
    # it was paid for packages that were about to be skipped too. That cost
    # grows as the R2 seed grows, which is backwards: the further hummingbird
    # converges, the more of each 6-hour run goes on confirming there is
    # nothing to do (tunaos-packages#410, #401).
    #
    # It is also more correct. The NVR this computes is compared against RPMs
    # an earlier run built; re-resolving the tag mid-run could answer from a
    # different image than the one that produced them.
    local nvr
    nvr=$(podman run --rm \
        --pull=missing \
        -v "$(dirname "$spec"):/specdir:Z" \
        "${BUILD_IMAGE}" \
        rpmspec -q "/specdir/${spec_basename}" \
            --define "dist ${DIST}" \
            --queryformat "%{NAME}-%{VERSION}-%{RELEASE}\n" | head -1)

    if [[ -z "$nvr" ]]; then
        return 1
    fi

    # Check if any RPM starting with this NVR exists in the local repo
    # We check for $nvr.rpm or $nvr.*.rpm
    if ls "${LOCAL_REPO}/${nvr}"*.rpm &>/dev/null; then
        echo "==> [${pkg_name}] Skipping: ${nvr} already exists in local repo"
        return 0
    fi

    # The published index is the durable record: a served NVR was built,
    # signed and synced by an earlier leg, and consumers resolve it from the
    # repo at priority 11 -- rebuilding it buys nothing. This is what makes
    # legs incremental ACROSS action-key moves, which partials cannot be.
    if [[ -n "${SERVED_NVRS_SET[$nvr]:-}" ]]; then
        echo "==> [${pkg_name}] Skipping: ${nvr} already served by the published index"
        return 0
    fi

    return 1
}
