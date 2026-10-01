# Tooling adapted from sandogasa

[slopfest/sandogasa](https://github.com/slopfest/sandogasa) is a Rust workspace of package tools for Fedora, CentOS, and Debian (Apache-2.0 OR MIT).
Several of its tools solve problems this factory faced.
Each adaptation below links to a previous incident.
We wrote the algorithms in Python without binary links.
The algorithms are small, and the factory already parses its repo metadata.

License: sandogasa uses Apache-2.0 OR MIT, which lets us adapt code here.
Per-file docstrings record the origin.
We brought over test vectors for the version comparator verbatim.

## What was adapted, and from where

| Factory piece | Adapted from | The incident it pins |
| --- | --- | --- |
| `scripts/rpm_vercmp.py` — librpm's rpmvercmp with `~`/`^`, EVR compare, constraint check | `sandogasa-rpmvercmp` crate (vectors carried over) | `FACTORY-STATUS.md` measures presence, not freshness; every version-aware check below needs this primitive |
| `scripts/preflight-buildrequires.py` `version_blocked` | ebranch `BlockedByBase` | libnotify `>= 0.8.7` unsatisfiable on both arches, found by mock 2.5 h in, twice (#480) |
| `scripts/preflight-buildrequires.py` `runtime_unsatisfied` | ebranch `check_installability` | gtkgreet → greetd, xfce4-pulseaudio-plugin → pulseaudio: runtime holes found at clean-install time after a 53-minute build (#480) |
| `scripts/check-published-hygiene.py` | hs-relmon `dupe-subpkgs` + `file-conflicts` | the createrepo_c `--update` stale-entry class (#358); 107 same-NEVRA pairs with differing checksums on xfce/10-stream (#471) |
| `scripts/check-reverse-deps.py` + the NEVER BREAK RDEPS gate in `publish-rpm-wave.sh` | ebranch `check-update` | the gnome50 bootstrap glib2 `Obsoletes` hijacking AppStream (run 32405815822); publishing a libnotify the factory could no longer rebuild |
| `scripts/extract-buildroot-manifest.py` + `scripts/diff-buildroots.py`, recorded per package by `build-chain.sh` into `artifacts/buildroots/` | koji-diff | the #480 libnotify buildroot diagnosis, reconstructed from issue comments when mock's root.log had the answer |
| `scripts/collect-cell-throughput.py` | koji-lag's measure-from-metadata approach | `docs/hummingbird-throughput.md` was a one-off hand scrape (it found concurrency 1.0 with `--jobs 2`); now re-runnable against any cell log |

## One level of support across targets

sandogasa focuses on Fedora and CentOS.
A direct port would create EL-centric tooling on a multi-target factory.
The adaptations build on a format-neutral layer in `scripts/repo_index.py`.
This layer provides one index format and one version comparator per package format.
Comparators disagree on real version strings, so each format uses its native rules.
`tests/test_target_tooling_parity.py` enforces this parity.

| Capability | rpm (el10/fedora/hummingbird/tumbleweed) | deb (ubuntu/debian) | pkg.tar.zst (arch) |
| --- | --- | --- | --- |
| index reader + version comparator | ✅ | ✅ | ✅ |
| served-index hygiene | ✅ | ✅ (no file lists in flat APT → file-conflict check inert, said in-tool) | ✅ once a `published_index` is declared |
| reverse-dep publish gate | ✅ `publish-rpm-wave.sh` | ✅ `publish-tideforge-debs.yml` (old-vs-new Packages) | ✅ `publish-tideforge-arch.yml` (old-vs-new .db) |
| buildroot manifests + differ | ✅ mock root.log / installed_pkgs | ✅ dpkg-query after build-dep | — no arch build chain exists yet; the differ already parses any manifest a future one writes |
| build/version/runtime preflight | ✅ (gap-engine manifests) | measured by the deb backport gap engine (`measure-deb-backport-gap.py`, pre-existing) | — no chain to preflight yet |
| throughput timers | ✅ mock's own timers | — the deb chain logs no per-package timer; add one there before extending the tool | — |

## Where each runs

- **Preflight** (`preflight-buildrequires.py`): manual check before dispatch of a chain.
- **Hygiene** (`check-published-hygiene.py`): checks the `published_index` contract across formats through `repo_index`.
- **Reverse-dep gates**: publishers refuse updates that break served packages.
- **Buildroot manifests**: chains for RPM and DEB record snapshots of buildroot packages.
- **Throughput**: run by hand against cell job logs for performance analysis.

## What was considered and NOT adapted

Recorded so the next reader does not re-survey the same ground:

- Forge tooling (Bodhi, Bugzilla, FESCo, Pagure ACLs, meetbot) has no role here.
- `dbranch` assumes Debian workflows with dist-git; the deb side uses `backport-deb-chain.yml`.
- We did not port ebranch shell-outs to `fedrq`: the factory parses `primary.xml` directly.
- koji-lag uses SQLite; stateless analysis of one log is enough for on-demand use.
