#!/usr/bin/env python3
"""Print NAME-VERSION-RELEASE for every package a published rpm-md index serves.

    list-served-nvrs.py https://repo.tunaos.org/hummingbird/20251124-x86_64/
    list-served-nvrs.py URL --except-names rebuild.txt

Output feeds `build-chain.sh --served-nvrs`: one NVR per line, deduplicated.
Epoch is deliberately dropped -- the skip compares against `rpmspec
--queryformat %{NAME}-%{VERSION}-%{RELEASE}`, which has no epoch either.

`--except-names FILE` withholds the NVRs of the SOURCE-or-BINARY names listed
in FILE, so the chain rebuilds them even though the index serves them. That is
the only way an already-served package ever gets rebuilt: "served" is the
chain's durable record of "exists", and a package built in the WRONG root
exists at exactly the NVR the right root would produce. The leak this exists
for is #629's: `libavutil-free`, `libavfilter-free` and `zvbi` were built on
the Rawhide buildroot, require `libm.so.6(GLIBC_2.44)` that Hummingbird's
glibc 2.43 cannot provide, and survived the 2026-09-02 move to the Fedora 44
root untouched -- still served, still uninstallable, on 2026-10-24.
`scripts/check-hummingbird-installability.py --print-rebuild-names` writes the
file; it names binary packages, and the skip is keyed on the SOURCE name the
chain builds, so both the binary name and its source are withheld.

An unreachable or empty index prints nothing and exits 0: a first publish
legitimately has nothing served, and the caller must build, not crash.
"""

from __future__ import annotations

import argparse
import gzip
import pathlib
import re
import subprocess


def fetch(url: str) -> bytes:
    # curl, not urllib: the CI egress proxy 403s urllib's requests.
    run = subprocess.run(["curl", "-sfL", "--max-time", "120", url],
                         capture_output=True)
    if run.returncode != 0:
        return b""
    return run.stdout


def nvrs(data: str, withheld: set[str]) -> list[str]:
    """Served NVRs in document order, minus everything `withheld` reaches.

    A withheld BINARY name withholds every binary of the SAME SOURCE BUILD,
    not just itself. The chain builds sources and keys its skip on the first
    binary `rpmspec -q` names for the spec: withholding `libavutil-free`
    alone leaves `ffmpeg-free` in the list, ffmpeg is skipped as served, and
    libavutil-free is never rebuilt -- the leak survives exactly as it has
    since September.
    """
    parsed = []
    for block in re.finditer(r'<package type="rpm">(.*?)</package>', data, re.S):
        body = block.group(1)
        name = re.search(r"<name>([^<]+)</name>", body)
        evr = re.search(r'ver="([^"]+)" rel="([^"]+)"', body)
        if not name or not evr:
            continue
        source = re.search(r"<rpm:sourcerpm>([^<]*)</rpm:sourcerpm>", body)
        source_nvr = ""
        if source and source.group(1).endswith(".src.rpm"):
            source_nvr = source.group(1)[: -len(".src.rpm")]
        parsed.append((name.group(1),
                       f"{name.group(1)}-{evr.group(1)}-{evr.group(2)}",
                       source_nvr))
    sources = {
        source_nvr for name, _, source_nvr in parsed
        if source_nvr and name in withheld
    }
    out, seen = [], set()
    for name, nvr, source_nvr in parsed:
        if name in withheld or (source_nvr and source_nvr in sources) or nvr in seen:
            continue
        seen.add(nvr)
        out.append(nvr)
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("baseurl")
    ap.add_argument("--except-names", type=pathlib.Path,
                    help="file of package names to withhold from the skip "
                         "list, so the chain rebuilds them (see the module "
                         "docstring: an ABI leak outlives every wave "
                         "otherwise)")
    args = ap.parse_args(argv)

    withheld: set[str] = set()
    if args.except_names:
        # No `if exists`: silently ignoring a missing exception list means
        # silently skipping the rebuild it exists to force.
        withheld = {line.strip() for line in
                    args.except_names.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.startswith("#")}

    base = args.baseurl.rstrip("/")
    repomd = fetch(f"{base}/repodata/repomd.xml").decode(errors="replace")
    m = re.search(r'href="(repodata/[^"]*primary\.xml[^"]*)"', repomd)
    if not m:
        return
    raw = fetch(f"{base}/{m.group(1)}")
    if not raw:
        return
    if m.group(1).endswith(".gz"):
        raw = gzip.decompress(raw)
    for nvr in nvrs(raw.decode(errors="replace"), withheld):
        print(nvr)


if __name__ == "__main__":
    main()
