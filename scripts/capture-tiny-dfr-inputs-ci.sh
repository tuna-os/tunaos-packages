#!/usr/bin/env bash
# CI input bootstrap only. This receipt is not a signed native package or a
# hardware-readiness result. No source or vendor digest is invented locally.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == true && "$(uname -m)" == aarch64 ]]
[[ "${GITHUB_REPOSITORY:-}" == tuna-os/tunaos-packages ]]
COMMIT=6267754535c16bd2c1006b946aa032561d8432b3
OUT="$PWD/.factory/tiny-dfr-inputs"
mkdir -p "$OUT"
export OUT COMMIT
# Resolve once; every subsequent inspect/pull uses the immutable child.
BASE=registry.opensuse.org/opensuse/tumbleweed
skopeo inspect --raw "docker://${BASE}:latest" >"$OUT/base-index.json"
CHILD=$(jq -er '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "arm64" and (.platform.variant // "v8") == "v8")] | if length == 1 then .[0].digest else error("unique native ARM child required") end' "$OUT/base-index.json")
[[ "$CHILD" =~ ^sha256:[0-9a-f]{64}$ ]]
export CHILD
skopeo inspect --raw "docker://${BASE}@${CHILD}" >"$OUT/base-child.json"
printf '%s  %s\n' "${CHILD#sha256:}" "$OUT/base-child.json" | sha256sum -c -
skopeo inspect --config --raw "docker://${BASE}@${CHILD}" >"$OUT/base-config.json"
CONFIG=$(jq -er '.config.digest' "$OUT/base-child.json")
printf '%s  %s\n' "${CONFIG#sha256:}" "$OUT/base-config.json" | sha256sum -c -
jq -e '.os == "linux" and .architecture == "arm64"' "$OUT/base-config.json"
sudo podman run --rm --interactive --pull=always --platform linux/arm64 \
	-v "$OUT:/output:rw" -v "$PWD/scripts:/scripts:ro" -e "COMMIT=$COMMIT" -e "CHILD=$CHILD" \
	-e "GITHUB_SHA=$GITHUB_SHA" -e "GITHUB_RUN_ID=$GITHUB_RUN_ID" -e "GITHUB_RUN_ATTEMPT=$GITHUB_RUN_ATTEMPT" \
	"${BASE}@${CHILD}" bash -euo pipefail -s <<'NATIVE'
exec > >(tee /output/native-bootstrap.log) 2>&1
[[ "$(uname -m)" == aarch64 ]]
. /etc/os-release
[[ "$ID" == opensuse-tumbleweed ]]
# Native zypper retains its default required GPG checks. No unsigned or
# foreign compiler packages and no rustup bootstrap are permitted.
bash /scripts/zypper-refresh-with-retry.sh
zypper --non-interactive install --no-recommends \
	rust cargo gcc pkgconf-pkg-config cairo-devel libinput-devel freetype2-devel \
	fontconfig-devel librsvg-devel libudev-devel libdrm-devel curl tar gzip python3
[[ "$(gcc -dumpmachine)" == aarch64-* ]]
rustc -vV | tee /output/rustc.txt
grep -qx 'host: aarch64-unknown-linux-gnu' /output/rustc.txt
cargo --version | tee /output/cargo.txt
gcc --version | tee /output/gcc.txt
rpm --eval '%{optflags}' | tee /output/native-optflags.txt
rpm -qa --qf '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n' | sort > /output/native-rpm-inventory.tsv
zypper --non-interactive repos --details > /output/native-repositories.txt
pkg-config --atleast-version=2.59 librsvg-2.0
for library in cairo libinput freetype2 fontconfig librsvg-2.0 libudev libdrm; do
	printf '%s ' "$library"
	pkg-config --modversion "$library"
done | tee /output/native-library-versions.txt
URL="https://github.com/AsahiLinux/tiny-dfr/archive/${COMMIT}.tar.gz"
curl --fail --location --proto '=https' --tlsv1.2 "$URL" -o /output/tiny-dfr-source.tar.gz
mkdir /source
tar -xzf /output/tiny-dfr-source.tar.gz --strip-components=1 -C /source
cd /source
test -s Cargo.lock
test -s LICENSE
test -s LICENSE.material
cp Cargo.lock /output/Cargo.lock
# cargo vendor validates registry checksums and uses the upstream locked
# closure. Save its complete replacement config, including any git sources.
mkdir -p .cargo
cargo vendor --locked vendor > .cargo/config.toml
cmp Cargo.lock /output/Cargo.lock
cargo metadata --locked --offline --format-version 1 > /output/cargo-metadata.json
cp .cargo/config.toml /output/vendor-config.toml
tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
	-cf - vendor .cargo | gzip -n > /output/tiny-dfr-vendor.tar.gz
cp LICENSE LICENSE.material /output/
python3 - <<'PY'
import hashlib, json, os, pathlib
p = pathlib.Path('/output')
def digest(name):
    return hashlib.sha256((p / name).read_bytes()).hexdigest()
value = {
    'schema': 1, 'scope': 'source-inputs-native-bootstrap', 'readiness': False,
    'upstream': {'repository': 'AsahiLinux/tiny-dfr', 'commit': os.environ['COMMIT'],
                 'url': f"https://github.com/AsahiLinux/tiny-dfr/archive/{os.environ['COMMIT']}.tar.gz",
                 'version': '0.3.7', 'license': 'MIT AND Apache-2.0'},
    'target': {'id': 'opensuse-tumbleweed', 'architecture': 'aarch64',
               'platform': 'linux/arm64', 'cpuPolicy': 'armv8-a',
               'cpuCompatibilityVerified': False},
    'producer': {'repository': 'tuna-os/tunaos-packages', 'sourceRevision': os.environ['GITHUB_SHA'],
                 'runId': int(os.environ['GITHUB_RUN_ID']),
                 'runAttempt': int(os.environ['GITHUB_RUN_ATTEMPT'])},
    'base': {'repository': 'registry.opensuse.org/opensuse/tumbleweed',
             'childDigest': os.environ['CHILD'],
             'configDigest': json.loads((p / 'base-child.json').read_text())['config']['digest']},
    'artifacts': {name: {'sha256': digest(name), 'size': (p / name).stat().st_size}
                  for name in ['tiny-dfr-source.tar.gz', 'tiny-dfr-vendor.tar.gz',
                               'vendor-config.toml', 'Cargo.lock', 'LICENSE', 'LICENSE.material',
                               'native-rpm-inventory.tsv', 'native-repositories.txt', 'rustc.txt',
                               'cargo.txt', 'gcc.txt', 'native-optflags.txt',
                               'native-library-versions.txt', 'cargo-metadata.json']},
}
(p / 'input-contract.json').write_text(json.dumps(value, indent=2) + '\n')
PY
NATIVE
