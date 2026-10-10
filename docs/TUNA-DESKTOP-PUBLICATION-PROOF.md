# Tuna Desktop publication proof

The Arch publisher retains `tuna-desktop-publication-<architecture>` for 90 days
when a wave includes `tuna-desktop`. Its `verdict.json` binds the installed served
package to the verified build's content-addressed ActionResult. A receipt passes
only when all of these match:

- The recipe tree, build image digest, selected architecture, renderer inputs,
  and source date epoch recompute the original build action key.
- The served installed archive's filename, version, architecture, size, and
  SHA-256 match that ActionResult.
- Pacman installs from the intended repository with required signatures, and
  GPG verifies the served detached signature against the pinned repository key.
- The publisher revision equals the verification checkout. The run URL,
  attempt, generation time, exact source/vendor URLs and checksums, and immutable
  package/signature identities accompany the result.

The receipt artifact includes installed observations, GPG status and detached
signature, and the build input/result records. The original build identity is
also retained separately as
`publish-arch-provenance-<architecture>-tuna-desktop`; metadata artifacts never
enter the binary wave staged for repository publication.

An absent, expired, failed, or incompatible receipt cannot qualify a release.
Rechecking removes an older passing `verdict.json` before validation. Consumers
must match the receipt to the exact package installed in their image and inspect
the authoritative workflow result and attempt. A matching version string or a
successful older source build is insufficient.

This extends the existing cached publisher; it does not change the recipe's
pinned source/vendor assets or dispatch a rebuild of the already published
`d443858` package. Previously completed runs do not acquire new receipts
retroactively. This document and synthetic contract tests are not evidence that
an actual publication receipt has passed.

Package verification covers publication and installation. Desktop capability,
GNOME performance, image greeter, upgrade, user-data recovery, and Corral results
are separate gates. The current recipe targets Arch; publication to other
TunaOS variants requires their appropriate package/image qualification.
