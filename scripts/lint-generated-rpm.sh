#!/usr/bin/env bash
# Lint generated RPMs, failing only on a curated set of findings.
set -euo pipefail

rpm_directory="${1:?usage: lint-generated-rpm.sh <directory-of-rpms>}"

fatal_checks=(
    unexpanded-macro
    specfile-error
    invalid-spec-name
    binary-or-shlib-defines-rpath
    no-changelogname-tag
    invalid-license
)

# Prefer the target's own repositories. Fedora and openSUSE carry rpmlint
# directly; EL may need EPEL. Capability detection keeps this generic for
# future RPM targets without treating Fedora as an EL derivative.
if command -v zypper >/dev/null 2>&1; then
    # `zypper install` refreshes implicitly, and a Tumbleweed mirror caught
    # mid-snapshot-rotation makes that refresh fail the whole repository --
    # which surfaces here as the confusing "No provider of 'rpmlint' found"
    # rather than as a network error. Refresh explicitly first, with retries.
    # Runs from /scripts, which every caller of this script already mounts.
    if [ -r /scripts/zypper-refresh-with-retry.sh ]; then
        bash /scripts/zypper-refresh-with-retry.sh
    fi
    zypper --non-interactive install rpmlint >/dev/null
else
    dnf_options=()
    if dnf repolist --enabled 2>/dev/null | grep -q 'fedora-cisco-openh264'; then
        dnf_options+=(--disablerepo=fedora-cisco-openh264)
    fi
    if dnf -y "${dnf_options[@]}" install rpmlint >/dev/null; then
        :
    else
        dnf -y install epel-release >/dev/null
        dnf -y install rpmlint >/dev/null
    fi
fi

mapfile -t rpms < <(find "$rpm_directory" -name '*.rpm' -type f | sort)
if [ "${#rpms[@]}" -eq 0 ]; then
    echo "lint-generated-rpm: no RPMs found under $rpm_directory" >&2
    exit 1
fi

report=$(mktemp)
rpmlint "${rpms[@]}" > "$report" 2>&1 || true

echo "===== rpmlint report (baseline; only curated checks are fatal) ====="
cat "$report"
echo "===================================================================="

if [ ! -s "$report" ]; then
    echo "lint-generated-rpm: rpmlint produced no output at all — treating as a" >&2
    echo "lint failure rather than a pass, because a silent linter is not a gate." >&2
    exit 1
fi

status=0
for check in "${fatal_checks[@]}"; do
    if grep -qE "(^|[[:space:]])(E|W): .*${check}" "$report"; then
        echo "lint-generated-rpm: FATAL finding '${check}' in generated RPM" >&2
        grep -E "(^|[[:space:]])(E|W): .*${check}" "$report" >&2
        status=1
    fi
done

exit "$status"
