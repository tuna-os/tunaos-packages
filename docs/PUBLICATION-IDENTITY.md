# Publication identity audit (#942)

Evaluates frostyard/repogen's `PackageIdentity` / `DetectDigestConflicts`
against the factory's publishers. No cross-distro version ordering is
proposed; native comparators in `scripts/repo_index.py` stay authoritative.

## Identities in use today

| Format | Index key (name, evr, arch) | Source/binary | Isolation between targets |
| --- | --- | --- | --- |
| rpm | `primary.xml` name, epoch:ver-rel, arch (`src` for source) | `arch == src` | one prefix per target/arch (`published_index`) |
| deb | `Package`, `Version`, `Architecture` | `Source` field only | per-target flat repo |
| pkg.tar.zst | `.db` entry name, evr, arch | none | per-target `.db` |
| gpkg | `CPV`, version, `ARCH` | none | per-target binhost |

## Findings

- `check-published-hygiene.py` flags a repeated NEVRA inside one prefix and
  a name served from several prefixes, but its rows carry no digest: the
  same NEVRA with different bytes in two prefixes reads as `redundant`.
- `publish-rpm-wave.sh` replaces a same-named RPM in place (`cp`) and then
  supersedes copies elsewhere by filename; it does not compare bytes, so a
  rebuild under an unchanged NEVRA silently changes served bytes.
- Equal names across formats, targets and architectures do not collide: each
  lives in its own prefix/index. `scripts/publication_identity.py` makes that
  qualification explicit and tested.

## What this change adds

`scripts/publication_identity.py`: format/target/arch/kind-qualified
`PackageIdentity`, `detect_digest_conflicts`, and `admit`, with tests in
`tests/test_publication_identity.py`. It is not yet wired into any
publisher; gating `publish-rpm-wave.sh` (reject or require a release bump)
changes publication behaviour and is left as the follow-up, which also needs
digests added to the index row readers.
