# Tuna Desktop publication proof

The Arch publisher retains `tuna-desktop-publication-<architecture>` for 90 days
when a wave includes `tuna-desktop`. Its `verdict.json` binds the installed served
package to the verified build's content-addressed ActionResult. A receipt passes
only when all of these match:

- The recipe, image digest, architecture and renderer inputs reproduce the original action key.
  The value of `source_date_epoch` must also match.
- The served installed archive's filename, version, architecture, size, and
  SHA-256 match that ActionResult.
- Pacman installs from the intended repository with required signatures.
  GPG verifies the detached signature against the pinned key.
- The publisher revision equals the verification checkout. The run URL,
  `run_attempt`, generation time, exact source/vendor URLs and checksums, and immutable
  package/signature identities accompany the result.

The receipt artifact includes installed observations, GPG status and detached
signature, and the build input/result records. The original build identity is
also retained separately as
`publish-arch-provenance-<architecture>-tuna-desktop`; metadata artifacts never
enter the binary wave staged for repository publication.

No release qualifies without a valid receipt.
Each check removes the old `verdict.json` before validation.
Consumers must match the receipt to the exact package installed in their image.
They must inspect the authoritative result and `run_attempt` from the workflow.
A version string or successful older build is insufficient.

This extends the existing cached publisher; it does not change the recipe's
pinned source/vendor assets or dispatch a rebuild of the already published
`d443858` package. Runs completed before this change do not acquire receipts
retroactively. This document and synthetic contract tests are not evidence that
an actual publication receipt has passed.

These checks cover publication and installation. Desktop capability,
GNOME performance, image greeter, upgrade, user-data recovery, and Corral results
are separate gates. The recipe supports Arch.
Other TunaOS variants need qualification of their packages and images.
