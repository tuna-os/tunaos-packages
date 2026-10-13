# EL10 native producer evidence

This is candidate tooling for two Skipjack EL10 cells, not an all-variant release or publication. The six-cell stager, Alma targets, production recipes, source/vendor pins, and published repositories are unchanged. The no-policy `admit_measured_closure` entry still rejects every report.

## Execution and trust

The existing registered `lint.yml` workflow has a manual `el10_native_closure` input, default **false**. Its ordinary source checks retain their existing gates. When explicitly enabled, the reusable producer first resolves one shared CentOS Stream 10 image pair. Each native job uses its exact platform image manifest digest and checks the actual runner architecture context against the container and native ELF probe. Image resolution alone is not native execution.

The native worker performs a new `cargo metadata --locked --offline --format-version 1` operation on hash-verified original source/vendor archives with a fresh private Cargo home. It preserves the locked source and actual metadata output. Its receipt names the current measurement run and attempt; it explicitly records that the original vendor-producing run is unknown. This receipt is accepted only through the explicit EL10 subset staging path. The default six-cell path still requires its original vendor-producing receipt.

All measurement commands share a 40-minute absolute phase budget. Each command has clipped operation limits, bounded stdout/stderr, a Linux subreaper, an unreaped session leader, and owned descendant retirement checks. Cleanup has a separate five-second limit. Kernel spawn and filesystem stalls are not hard real-time guarantees. Unconfirmed retirement refuses completion; this is not persistent quarantine after the observer process exits. The CI container lifecycle is handled by the GitHub runner, not independently attested by this helper.

Native jobs run local byte and semantic checks before uploading attempt-specific artifacts. These checks cannot authenticate an in-progress run. After the original workflow completes successfully, the separate observer is invoked with an explicitly reviewed head and run:

```sh
python scripts/el10-measure-closure.py admit-run \
  --head-sha EXACT_REVIEWED_COMMIT \
  --run-id ORIGINAL_COMPLETED_NATIVE_RUN \
  --out /owned/fresh/external-directory
```

The observer uses canonical GitHub API transport, exact original repository/event/workflow/run/attempt/job identities, source Git blob bytes, original artifact API size/digest, bounded archive extraction, and both native leg reports. It refuses source/requirement drift, wrong architecture, incomplete jobs, skipped native steps, mixed attempts, changed source bytes, changed ZIPs, or substituted report payloads. The observer executing files must match the original producer commit. It never picks a latest run or grants trust to a report-supplied keyring. Its report describes what the producing run observed; it does not claim the repository remains unchanged afterward.

## Keys and supplier evidence

The source policy pins public certificate bytes by SHA-256 and full primary/signing fingerprints. Official identity references are [CentOS keys](https://www.centos.org/keys/) and [Fedora security](https://fedoraproject.org/security/). GPG checks certificate self-signatures and signing subkey bindings in private homes. RPM verification uses private per-certificate databases; digest-only success is insufficient.

CentOS Extras is restricted to **epel-release bootstrap only**. The actual DNF repository configuration sets `includepkgs=epel-release`; native observation and validation reject a broadened filter. Extras cannot authenticate general providers. CentOS BaseOS/AppStream/CRB, EPEL 10, and factory packages retain distinct pinned key scopes. A missing package or metadata signature is a genuine refusal; this tooling never disables `gpgcheck` or `repo_gpgcheck`.

For every requirement, the producer retains:

- Actual installed DNF history origin and the exact matching available name, epoch, version, release, architecture, and repository.
- Repo-pinned downloaded RPM bytes, installed/downloaded header identity, strong DNF checksum, and the native signature verifier result.
- The actual signed repomd, detached signature, GPG status, strong primary checksum descriptor, and exact provider membership/checksum in the original primary XML.
- Actual installed ABI file hashes and native RPM owners, readable headers and typelibs, loaded libraries, SDK compile/run verdicts, grounded symbols and versions, or appropriate named-package smoke checks.

`noarch` suppliers are allowed only with an observed architecture-neutral payload; wrong-architecture ELF providers are refused. Compressed primary XML is decoded in an owned bounded child and rechecked against the original compressed byte hash. SHA-512 metadata is represented honestly; no declared SHA-256 value is invented.

The report itself is not signed by an RPM key. Its authenticity comes from independent original CI/API/source/artifact custody. Per-RPM and index signatures authenticate their own bytes. Immutable observations and an external final bundle manifest avoid a self-referential report hash.

## Qualification limits

The source tests use explicitly synthetic API/header/signature controls where a native operation is not executed. Real Linux subprocess and gzip/XML retirement controls run separately. None of these controls stands in for native DNF, Cargo, GPG, SDK, or package execution.

The frozen production recipe at the current baseline lacks the portal/cursor installation prerequisites required by the stager. Its native job must remain red at that source gap. Native repository signature availability, current provider closure, both actual architecture legs, and original successful-run admission remain unqualified until CI executes them. No all-14 package/image/session/auth/portal/visual acceptance, signing promotion, source pin change, or publication follows from these source checks.
