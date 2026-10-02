#!/usr/bin/env bash
#
# Build RPMs using mock inside containers
#
# Usage:
#   ./scripts/build-local.sh hello-world fedora-40-x86_64
#   ./scripts/build-local.sh hello-world alma-9-x86_64

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SPEC_FILE=""
TARGET="${2:-fedora-40-x86_64}"
# MOCK_CONFIG is defined in target-specific files if needed
# Use podman if available, fall back to docker
if command -v podman &>/dev/null; then
    CONTAINER_RUNTIME="podman"
elif command -v docker &>/dev/null; then
    CONTAINER_RUNTIME="docker"
else
    echo "Error: Neither podman nor docker found"
    exit 1
fi

CONTAINER_IMAGE="mock-builder:fedora-43"

build_container() {
    echo "=== Building container image ==="
    $CONTAINER_RUNTIME build -t "$CONTAINER_IMAGE" "$PROJECT_DIR"
}
OUTPUT_DIR="$PROJECT_DIR/output/$TARGET"

usage() {
    echo "Usage: $0 <package-name> [target]"
    echo ""
    echo "Examples:"
    echo "  $0 hello-world fedora-44-x86_64"
    echo "  $0 hello-world fedora-eln-x86_64"
    echo "  $0 hello-world almalinux-10-x86_64"
    echo "  $0 hello-world centos-stream-10-x86_64"
    echo ""
    echo "Available targets:"
    echo "  fedora-44-x86_64"
    echo "  fedora-44-aarch64"
    echo "  fedora-eln-x86_64"
    echo "  fedora-eln-aarch64"
    echo "  almalinux-10-x86_64"
    echo "  almalinux-10-x86_64_v2"
    echo "  almalinux-10-aarch64"
    echo "  centos-stream-10-x86_64"
    echo "  centos-stream-10-aarch64"
}

find_spec() {
    local pkg="$1"
    local spec="$PROJECT_DIR/src/$pkg.spec"
    
    if [ -f "$spec" ]; then
        SPEC_FILE="$spec"
        return 0
    fi
    
    # Try to find spec in src/
    # shellcheck disable=SC2012
    spec=$(ls "$PROJECT_DIR/src/"*.spec 2>/dev/null | head -n1 || true)
    if [ -n "$spec" ]; then
        SPEC_FILE="$spec"
        return 0
    fi
    
    return 1
}

build_srpm() {
    local pkg="$1"
    local spec="$2"
    local topdir="$PROJECT_DIR/build"
    
    echo "=== Creating source RPM ==="
    
    # Setup build directories
    mkdir -p "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SRPMS,SPECS}
    
    # Copy spec and sources
    cp "$spec" "$topdir/SPECS/"
    
    # Create tarball if needed
    # (Note: This still runs on host, but for hello-world it's fine.
    # For more complex specs it might need refinement)
    local name
    name=$(basename "$spec" .spec)
    
    if [ -d "$PROJECT_DIR/src/$name" ]; then
        tar -czf "$topdir/SOURCES/$name.tar.gz" -C "$PROJECT_DIR/src" "$name"
    fi
    
    # Build SRPM inside container
    $CONTAINER_RUNTIME run --rm \
        -v "$PROJECT_DIR:/workspace" \
        -w /workspace \
        "$CONTAINER_IMAGE" \
        rpmbuild --define "_topdir /workspace/build" -bs "/workspace/build/SPECS/$(basename "$spec")"
    
    echo "SRPM created: $topdir/SRPMS/"
}

build_with_mock() {
    local srpm="$1"
    local target="$2"
    
    echo "=== Building with mock: $target ==="
    
    # Check for custom mock config
    local mock_opts=""
    if [ -f "$PROJECT_DIR/mock/${target}.cfg" ]; then
        mock_opts="-r $target --configdir=/workspace/mock"
        echo "Using custom config: mock/${target}.cfg"
    else
        mock_opts="-r $target"
    fi
    
    # Ensure container exists
    if ! $CONTAINER_RUNTIME images | grep -q "$CONTAINER_IMAGE"; then
        build_container
    fi
    
    # Run mock in container
    $CONTAINER_RUNTIME run --rm \
        -v "$PROJECT_DIR:/workspace" \
        -w /workspace \
        -e HOME=/tmp \
        "$CONTAINER_IMAGE" \
        sh -c "
            mock $mock_opts --init && \
            mock $mock_opts --install rpm-build && \
            mock $mock_opts --srpm $srpm && \
            mock $mock_opts --build --no-clean --no-cleanup-after
        "
    
    # Copy results
    mkdir -p "$OUTPUT_DIR"
    $CONTAINER_RUNTIME run --rm \
        -v "$PROJECT_DIR:/workspace" \
        -w /workspace \
        "$CONTAINER_IMAGE" \
        sh -c "cp /var/lib/mock/$target/result/*.rpm /workspace/output/$target/ 2>/dev/null || true"
    
    echo "=== Results in $OUTPUT_DIR ==="
    ls -la "$OUTPUT_DIR" || echo "No RPMs found"
}

main() {
    if [ -z "${1:-}" ]; then
        usage
        exit 1
    fi
    
    local pkg="$1"
    
    echo "Building: $pkg for $TARGET"
    echo ""
    
    # Build container if needed
    if ! $CONTAINER_RUNTIME images | grep -q "$CONTAINER_IMAGE"; then
        build_container
    fi
    
    # Find spec file
    if ! find_spec "$pkg"; then
        echo "Error: Spec file not found for package: $pkg"
        exit 1
    fi
    
    echo "Using spec: $SPEC_FILE"
    
    # Build SRPM
    build_srpm "$pkg" "$SPEC_FILE"
    
    # Find the SRPM
    local srpm
    # shellcheck disable=SC2012
    srpm=$(ls "$PROJECT_DIR/build/SRPMS/"*.src.rpm 2>/dev/null | head -n1)
    
    if [ -z "$srpm" ]; then
        echo "Error: SRPM not found"
        exit 1
    fi
    
    echo "SRPM: $srpm"
    
    # Build with mock
    build_with_mock "$srpm" "$TARGET"
    
    echo ""
    echo "=== Build complete ==="
}

main "$@"
