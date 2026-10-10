#!/usr/bin/env bash
# Source after recipe environment exports and call tunaos_alma_compiler_policy
# with actual RPM _target_cpu. Writes effective flags; this is not ISA proof.
tunaos_alma_compiler_policy() {
    local architecture="${1:?actual RPM target CPU required}"
    local baseline rust_cpu
    case "$architecture" in
        x86_64) baseline=x86-64-v2; rust_cpu=x86-64-v2 ;;
        aarch64) baseline=armv8-a; rust_cpu=generic ;;
        *) echo 'Unsupported Alma compiler architecture' >&2; return 1 ;;
    esac
    export TUNAOS_CPU_BASELINE="$baseline" TUNAOS_RUST_CPU="$rust_cpu"
    if ! python3 - <<'PY'
import os, re, shlex, sys
baseline = os.environ['TUNAOS_CPU_BASELINE']
rust = os.environ['TUNAOS_RUST_CPU']
for name in ('CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'FFLAGS', 'FCFLAGS', 'RUSTFLAGS', 'LDFLAGS', 'CC', 'CXX', 'FC'):
    value = os.environ.get(name, '')
    try:
        tokens = shlex.split(value)
    except ValueError:
        sys.exit('Malformed compiler flags: ' + name)
    for token in tokens:
        # Alma's AArch64 hardening uses backward-compatible PAC/BTI hint
        # instructions with -march=armv8-a. It does not raise the ISA floor.
        if baseline == 'armv8-a' and token == '-mbranch-protection=standard':
            continue
        for setting in re.findall(r'(?:-march=|-mcpu=|target-cpu=)([^\s]+)', token):
            if setting != (rust if 'target-cpu=' in token else baseline):
                sys.exit('Incompatible or unknown CPU override: ' + name)
        if re.search(r'(?<!\w)-m(?!arch=|cpu=|tune=|64$|no-|tls-dialect=gnu2$)[a-zA-Z]', token) or '+' in token and 'target-feature' in value:
            sys.exit('Unproved CPU extension override: ' + name)
if os.environ.get('CARGO_ENCODED_RUSTFLAGS'):
    sys.exit('Encoded Rust flags require measured adapter')
if os.environ.get('GOAMD64') not in (None, '', 'v2'):
    sys.exit('Incompatible Go CPU override')
PY
    then
        return 1
    fi
    export CFLAGS="${CFLAGS:-} -march=$baseline"
    export CXXFLAGS="${CXXFLAGS:-} -march=$baseline"
    export FFLAGS="${FFLAGS:-} -march=$baseline"
    export FCFLAGS="${FCFLAGS:-} -march=$baseline"
    export RUSTFLAGS="${RUSTFLAGS:-} -C target-cpu=$rust_cpu"
    if [[ $architecture == x86_64 ]]; then export GOAMD64=v2; else unset GOAMD64; fi
    python3 - <<'PY'
import json, os, pathlib
root = pathlib.Path(os.environ['TUNAOS_COMPILER_EVIDENCE_DIR'])
root.mkdir(parents=True, exist_ok=True)
fields = ('CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'FFLAGS', 'FCFLAGS', 'RUSTFLAGS', 'LDFLAGS', 'GOAMD64')
(root / 'effective-flags.json').write_text(json.dumps({'schemaVersion': 1,
    'cpuBaseline': os.environ['TUNAOS_CPU_BASELINE'],
    'flags': {key: os.environ.get(key) for key in fields}, 'readiness': False}, sort_keys=True) + '\n')
PY
}
