#!/usr/bin/env bash
# Verify one unified matrix row. Package-format mechanics live here; session
# requirements remain recipe/queue data and are invoked through their gates.
set -eEuo pipefail

out=${OUT_DIR:-"$PWD/.factory/${CELL_ID:?}"}
artifacts="$out/artifacts"
test -d "$artifacts"

# The target's runtime repo set (#446): the clean-install verify must resolve
# against the same repos a produced image enables (for el10: crb + epel, per
# tunaOS build_scripts/10-base-packages.sh), or real-world-installable
# packages fail the gate on deps the image serves fine — run 32382594650
# failed every cosmic el10 cell on EPEL-served gnome-icon-theme/libdav1d.so.7.
# Empty for targets that declare none; only the dnf paths consume it today.
system_repos="$(python3 - "${TARGET:-}" <<'PY'
import pathlib, sys, yaml
d = yaml.safe_load(pathlib.Path("manifests/package-factory.yaml").read_text()) or {}
t = d.get("targets", {}).get(sys.argv[1]) or {}
print(" ".join(t.get("system_repositories") or []))
PY
)"

# The target contract's `published_index` (served read URL, distinct from
# `r2_path`) is added as a lower-priority repo so the local cell artifacts win
# and the published repo only fills in deps. Space-separated: an arch may
# declare several indexes (#467). URLs never contain whitespace, so
# word-splitting in the container is safe.
#
# Resolved ONCE above BOTH the engine branch and the format case. It was
# assigned below the build-chain branch, so build-chain cells got no published
# repo at all -- which is why xfce-el10 could not resolve the greetd that this
# factory builds and serves at rpm/el10/{arch} (#482). It had already been
# hoisted once, past the rpm branch, for the same reason on deb.
published_index="$(python3 scripts/published_index.py "$TARGET" "${ARCHITECTURE:-}" --join)"

if [[ ${ENGINE:?} == build-chain ]]; then
  mapfile -d '' rpms < <(find "$artifacts" -type f -name '*.rpm' ! -name '*.src.rpm' -print0)
  ((${#rpms[@]} > 0)) || { echo "native queue produced no RPMs" >&2; exit 1; }
  docker run --rm --entrypoint /bin/bash --volume "$artifacts:/artifacts:ro" \
    --volume "$PWD/scripts:/scripts:ro" "${IMAGE:?}" \
    /scripts/lint-generated-rpm.sh /artifacts
  docker run --rm --entrypoint /bin/bash \
    --env TARGET="${TARGET:?}" --env SYSTEM_REPOS="$system_repos" \
    --env PUBLISHED_INDEX="$published_index" \
    --volume "$artifacts:/artifacts:ro" "${IMAGE:?}" -lc '
    set -euo pipefail
    for sysrepo in ${SYSTEM_REPOS:-}; do
      case $sysrepo in
        epel) dnf -y install epel-release ;;
        *) dnf -y install "dnf-command(config-manager)"
           dnf config-manager --set-enabled "$sysrepo" ;;
      esac
    done
    dnf -y install createrepo_c
    mkdir /factory-repo
    find /artifacts -maxdepth 1 -type f -name "*.rpm" ! -name "*.src.rpm" -exec cp -t /factory-repo {} +
    createrepo_c /factory-repo
    # Same shape as the tideforge branch: priority 50 so the local cell
    # artifacts (priority 1) always win and the published repo only fills in
    # dependencies this chain does not build itself.
    published_args=()
    published_n=0
    for published_url in ${PUBLISHED_INDEX:-}; do
      published_args+=(--repofrompath "tunaos${published_n},${published_url}"
                       "--setopt=tunaos${published_n}.priority=50"
                       --enablerepo="tunaos${published_n}")
      published_n=$((published_n + 1))
    done
    # ${a[@]+"${a[@]}"} and not "${a[@]}": under `set -u` an empty array
    # expansion is an unbound-variable error on bash < 4.4.
    dnf -y install --nogpgcheck --repofrompath factory,file:///factory-repo \
      --setopt=factory.priority=1 --enablerepo=factory \
      ${published_args[@]+"${published_args[@]}"} \
      /factory-repo/*.rpm
    mapfile -t names < <(rpm -qp --qf "%{NAME}\n" /factory-repo/*.rpm | sort -u)
    rpm -q "${names[@]}"
    rpm -V "${names[@]}"
  '
  exit 0
fi

recipe=${RECIPE:?}
if python3 - "$recipe" <<'PY'
import pathlib, sys, yaml
recipe = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}
raise SystemExit(0 if recipe.get("verify") else 1)
PY
then
  python3 scripts/tideforge.py verify "$recipe" --target "${TARGET:?}" --field smoke > "$out/smoke.sh"
  install_name=$(python3 scripts/tideforge.py verify "$recipe" --target "$TARGET" --field install_name)
else
  printf 'true\n' > "$out/smoke.sh"
  install_name=$(python3 - "$recipe" <<'PY'
import pathlib, sys, yaml
recipe = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}
print(recipe.get("name") or pathlib.Path(sys.argv[1]).parent.name)
PY
)
fi

# Cross-cell dependency resolution (#440): a clean-install verify of a package
# whose runtime deps are themselves in the gap (e.g. niri needs libseat) can
# only resolve them from the PUBLISHED factory repo, built by earlier waves.
# The target contract's `published_index` (served read URL, distinct from
# `r2_path`) is added as a lower-priority repo so the local cell artifacts win
# and the published repo only fills in deps. Space-separated: an arch may
# declare several indexes (#467) — el10 x86_64 has the tideforge mirror and the
# xfce build-chain prefix. URLs never contain whitespace, so word-splitting in
# the container is safe.
#
case ${FORMAT:?} in
  rpm)
    docker run --rm --entrypoint /bin/bash --volume "$artifacts:/artifacts:ro" \
      --volume "$PWD/scripts:/scripts:ro" "${IMAGE:?}" \
      /scripts/lint-generated-rpm.sh /artifacts
    docker run --rm --entrypoint /bin/bash --env INSTALL_NAME="$install_name" \
      --env PUBLISHED_INDEX="$published_index" --env SYSTEM_REPOS="$system_repos" \
      --volume "$artifacts:/artifacts:ro" --volume "$out/smoke.sh:/smoke.sh:ro" \
      --volume "$PWD/scripts:/scripts:ro" "${IMAGE:?}" -lc '
        set -euo pipefail
        mkdir /factory-repo
        cp /artifacts/*.rpm /factory-repo/
        if command -v zypper >/dev/null; then
          zypper --non-interactive install createrepo_c
          createrepo_c /factory-repo
          zypper --non-interactive addrepo --no-gpgcheck --priority 1 file:///factory-repo tideforge
          published_n=0
          for published_url in ${PUBLISHED_INDEX:-}; do
            zypper --non-interactive addrepo --no-gpgcheck --priority 50 \
              "$published_url" "tunaos-published-${published_n}"
            published_n=$((published_n + 1))
          done
          bash /scripts/zypper-refresh-with-retry.sh
          zypper --non-interactive --no-gpg-checks install "$INSTALL_NAME"
        else
          for sysrepo in ${SYSTEM_REPOS:-}; do
            case $sysrepo in
              epel) dnf -y install epel-release ;;
              *) dnf -y install "dnf-command(config-manager)"
                 dnf config-manager --set-enabled "$sysrepo" ;;
            esac
          done
          dnf -y install createrepo_c
          createrepo_c /factory-repo
          published_args=()
          published_n=0
          for published_url in ${PUBLISHED_INDEX:-}; do
            published_args+=(--repofrompath "tunaos${published_n},${published_url}"
                             "--setopt=tunaos${published_n}.priority=50"
                             --enablerepo="tunaos${published_n}")
            published_n=$((published_n + 1))
          done
          # ${a[@]+"${a[@]}"} and not "${a[@]}": under `set -u` an empty
          # array expansion is an unbound-variable error on bash < 4.4, and
          # the probe images are not all bash 5.
          dnf -y install --nogpgcheck --repofrompath tideforge,file:///factory-repo \
            --setopt=tideforge.priority=1 --enablerepo=tideforge \
            ${published_args[@]+"${published_args[@]}"} \
            "$INSTALL_NAME"
        fi
        rpm -q "$INSTALL_NAME"
        bash /smoke.sh
      '
    ;;
  deb)
    docker run --rm --volume "$artifacts:/artifacts:ro" \
      --volume "$PWD/scripts:/scripts:ro" "${IMAGE:?}" \
      bash /scripts/lint-generated-deb.sh /artifacts
    docker run --rm --env INSTALL_NAME="$install_name" \
      --env PUBLISHED_INDEX="$published_index" \
      --volume "$artifacts:/artifacts:ro" --volume "$out/smoke.sh:/smoke.sh:ro" \
      "${IMAGE:?}" bash -lc '
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        # ca-certificates BEFORE the HTTPS indexes are added, not after. apt
        # skips an unreachable source with a WARNING and exits 0, so adding
        # them first turns a missing CA bundle into an unexplained missing
        # package a hundred lines later. Same ordering as the build container.
        apt-get install -y --no-install-recommends dpkg-dev pkg-config ca-certificates
        mkdir /repo && cp /artifacts/*.deb /repo/ && cd /repo
        dpkg-scanpackages . /dev/null | gzip -9c > Packages.gz
        printf "deb [trusted=yes] file:/repo ./\n" > /etc/apt/sources.list.d/tideforge.list
        printf "Package: *\nPin: origin \"\"\nPin-Priority: 1001\n" > /etc/apt/preferences.d/tideforge.pref
        # Both RPM branches above already declare the published indexes here;
        # the deb branch did not, so a package whose runtime closure includes a
        # factory-PUBLISHED deb could never be verified. quickshell links
        # libcpptrace.so.1, dpkg-shlibdeps resolved that to libcpptrace-dev,
        # and the install died with "Depends libcpptrace-dev (>= 1.0.4) but
        # none of the choices are installable: [no choices]" -- against a
        # package that is built, published and served.
        #
        # Priority 100 is below apt default 500 and far below the local
        # repo pin of 1001: the published index may only ever FILL A GAP, never
        # outrank the artifact under test or the distro archive.
        published_n=0
        for published_url in ${PUBLISHED_INDEX:-}; do
          printf "deb [trusted=yes] %s ./\n" "$published_url" \
            > "/etc/apt/sources.list.d/tunaos-published-${published_n}.list"
          published_host=$(printf "%s" "$published_url" | sed -E "s#^[a-z]+://([^/]+).*#\\1#")
          printf "Package: *\nPin: origin \"%s\"\nPin-Priority: 100\n" "$published_host" \
            > "/etc/apt/preferences.d/tunaos-published-${published_n}.pref"
          published_n=$((published_n + 1))
        done
        apt-get update > /tmp/apt-update.log 2>&1 || { cat /tmp/apt-update.log >&2; exit 1; }
        cat /tmp/apt-update.log
        if grep -q "Failed to fetch" /tmp/apt-update.log; then
          echo "ERROR: a declared published index could not be fetched (see above)." >&2
          exit 1
        fi
        apt-get install -y "$INSTALL_NAME"
        dpkg-query -W "$INSTALL_NAME"
        bash /smoke.sh
      '
    ;;
  pkg.tar.zst)
    python3 scripts/validate-built-arch-package.py "$recipe" "$out/package-info.txt"
    docker run --rm --user root --volume "$artifacts:/artifacts:ro" \
      --volume "$out/smoke.sh:/smoke.sh:ro" --volume "$PWD/scripts:/scripts:ro" \
      "${IMAGE:?}" bash /scripts/arch-clean-install.sh "$install_name" /artifacts bash /smoke.sh
    ;;
  *) echo "unsupported package format: $FORMAT" >&2; exit 2 ;;
esac
