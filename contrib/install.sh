#!/usr/bin/env bash
#
# Tuna OS RPM Repository
#
# Usage:
#   curl -sSL https://repo.tunaos.org/install.sh | sudo bash

set -euo pipefail

REPO_URL="${REPO_URL:-https://repo.tunaos.org}"
REPO_NAME="${REPO_NAME:-tuna-os}"

detect_distro() {
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "$ID" in
            fedora)
                echo "fedora"
                ;;
            rhel|centos|alma|rocky)
                echo "el"
                ;;
            *)
                echo "unknown"
                ;;
        esac
    else
        echo "unknown"
    fi
}

detect_version() {
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "$ID" in
            fedora)
                echo "$VERSION_ID"
                ;;
            rhel|centos|alma|rocky)
                echo "$VERSION_ID"
                ;;
            *)
                echo "9"
                ;;
        esac
    else
        echo "9"
    fi
}

install_gpg_key() {
    echo "Installing GPG key..."
    curl -sSLo /etc/pki/rpm-gpg/RPM-GPG-KEY-tunaos "$REPO_URL/public.gpg"
    rpm --import /etc/pki/rpm-gpg/RPM-GPG-KEY-tunaos 2>/dev/null || true
}

install_repo_file() {
    local version
    version=$(detect_version)
    
    # gpgcheck=1 verifies each RPM against the tuna-os signing key -- every
    # repo.tunaos.org publish pipeline signs its RPMs with `rpmsign
    # --addsign` before upload (see tuna-os/tunaos-packages#394).
    # repo_gpgcheck=1 verifies repodata/repomd.xml against the detached
    # repomd.xml.asc that every publisher writes (scripts/sign-repomd.sh), so
    # a replayed or altered index is rejected (#509).
    cat > "/etc/yum.repos.d/${REPO_NAME}.repo" << EOF
[${REPO_NAME}]
name=Tuna OS - \$releasever
baseurl=${REPO_URL}/repo/\$releasever/\$basearch/
enabled=1
gpgcheck=1
gpgkey=${REPO_URL}/public.gpg
repo_gpgcheck=1
metadata_expire=3600
priority=10
EOF

    echo "Repository installed for version: $version"
}

verify_installation() {
    echo "Verifying installation..."
    if dnf repolist "$REPO_NAME" >/dev/null 2>&1; then
        echo "Repository enabled successfully!"
        dnf repolist "$REPO_NAME"
    else
        echo "Warning: Repository may not be enabled. Try:"
        echo "  dnf config-manager --enable ${REPO_NAME}"
    fi
}

main() {
    echo "Installing ${REPO_NAME} repository from ${REPO_URL}..."
    
    install_gpg_key
    install_repo_file
    verify_installation
    
    echo ""
    echo "Installation complete!"
    echo "To install packages from this repository, run:"
    echo "  dnf install <package-name>"
    echo ""
    echo "To update all packages from this repository:"
    echo "  dnf update --repo=${REPO_NAME}"
}

main "$@"
