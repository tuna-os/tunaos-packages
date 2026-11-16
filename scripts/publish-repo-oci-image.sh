#!/usr/bin/env bash
# Publish a synced family index as a FROM-scratch OCI image with one layer:
# the tree at /repository (the utah-packages shape tunaOS bind-mounts).
#
# Usage: publish-repo-oci-image.sh <repo-dir> <image:tag> <authfile> <digestfile> <layout-dir>
#
# Written without containers/storage on purpose. `podman build` with
# `COPY repo /repository` holds three copies of the tree on one disk: the
# synced repo/, the container layer and the blob that commit writes to
# /var/tmp. The hummingbird prefix is 32 GB, so publish run 43641087208 ran
# out of space at commit on a 145 GB runner whose /mnt is the same disk. Here
# the layer is one uncompressed tar (RPMs do not compress), written once into
# an OCI layout, and skopeo streams that blob to the registry as is.
set -euo pipefail

src="${1:?repo dir}"
ref="${2:?image:tag}"
authfile="${3:?authfile}"
digestfile="${4:?digest file}"
layout="${5:?layout dir}"

[[ -d "${src}" ]] || {
	echo "::error::${src} is not a directory" >&2
	exit 1
}
case "$(uname -m)" in
x86_64) arch=amd64 ;;
aarch64) arch=arm64 ;;
*)
	echo "::error::unsupported runner architecture $(uname -m)" >&2
	exit 1
	;;
esac

rm -rf "${layout}"
mkdir -p "${layout}/blobs/sha256"
printf '{"imageLayoutVersion":"1.0.0"}\n' >"${layout}/oci-layout"

# blob <file>: move a file into the layout under its digest; print the digest.
blob() {
	local digest
	digest=$(sha256sum "$1" | cut -d' ' -f1)
	mv "$1" "${layout}/blobs/sha256/${digest}"
	printf '%s' "${digest}"
}

parent=$(dirname "$(realpath "${src}")")
name=$(basename "$(realpath "${src}")")
tar --sort=name --owner=0 --group=0 --numeric-owner \
	--transform "s,^${name},repository," \
	-C "${parent}" -cf "${layout}/layer.tar" "${name}"
layer_size=$(stat -c %s "${layout}/layer.tar")
layer=$(blob "${layout}/layer.tar")

# An uncompressed layer's digest is its diff_id.
printf '{"architecture":"%s","os":"linux","config":{},"rootfs":{"type":"layers","diff_ids":["sha256:%s"]}}' \
	"${arch}" "${layer}" >"${layout}/config.json"
config_size=$(stat -c %s "${layout}/config.json")
config=$(blob "${layout}/config.json")

printf '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"sha256:%s","size":%s},"layers":[{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:%s","size":%s}]}' \
	"${config}" "${config_size}" "${layer}" "${layer_size}" >"${layout}/manifest.json"
manifest_size=$(stat -c %s "${layout}/manifest.json")
manifest=$(blob "${layout}/manifest.json")

printf '{"schemaVersion":2,"manifests":[{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"sha256:%s","size":%s,"annotations":{"org.opencontainers.image.ref.name":"published"}}]}\n' \
	"${manifest}" "${manifest_size}" >"${layout}/index.json"

skopeo copy --authfile "${authfile}" --digestfile "${digestfile}" \
	"oci:${layout}:published" "docker://${ref}"
