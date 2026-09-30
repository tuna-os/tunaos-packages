# GNOME 50 repo publish

The endpoint [repo.tunaos.org](https://repo.tunaos.org/gnome50/10-stream-x86_64/) serves the GNOME 50 family index for EL10.
The workflow `publish-build-chain-rpms.yml` writes this index for the cell `gnome50-el10-x86_64`.
The same run publishes an OCI image, `ghcr.io/tuna-os/tunaos-packages:gnome50-el10-x86_64`, signed with cosign by digest (`docs/PACKAGE_FACTORY.md`, "Promotion contract").

## What was there before the first factory publish (2026-09-03)

The prefix was not empty and did not come from the factory.
The workflow `refresh-gnome50-r2.yml` (runs 30926227247 and 30928751255, 2026-08-04) downloaded the `jreilly1821/c10s-gnome-50` COPR repository.
It synchronized the files into `bluefin/gnome50/10-stream-x86_64/` and `bluefin/repo/10-x86_64/`.
Those RPMs carry the signature of the COPR project key (`99b9f29ec528e021`, vendor `Fedora Copr - user jreilly1821`).
A consumer that trusts `public.gpg` with `gpgcheck=1` cannot install these packages.
For example, tunaOS run 33750514082 failed with: `GPG check FAILED ... Public key for glib2-2.88.0-4.el10.x86_64.rpm is not installed`.

The factory cell `gnome50-el10-x86_64` had not published before.
Its catalog `r2_path` was `repo/10-x86_64`, the tideforge mirror prefix.
The script `scripts/plan-build-chain-publish.py` refuses this prefix by name.
Two sync writers to one prefix delete packages from each other (run 33751204743 failed at plan for that reason).
The weekly build run on 2026-08-30 (33303057118) built 57 of 58 packages across 19 tiers.
The spec for `input-remapper` lacked `BuildRequires: systemd-rpm-macros`.

Maintainer directive, 2026-09-03: nothing below GNOME 50 ships in tunaOS, and no more COPR -- build in GitHub, consume like `projectbluefin/utah-packages`.

## What the publisher does now

1. `manifests/package-builds.yaml` gives `gnome50-el10-x86_64` its own prefix, `gnome50/10-stream-x86_64`, the shape `gnome51/` and `xfce/` already use.
2. `scripts/publish-rpm-wave.sh --evict-foreign` removes foreign RPMs. The publisher key must sign each RPM header before index creation. The script does not evict packages if key verification fails.
3. The sync-up deletes the COPR mirror from the bucket. Next, `createrepo_c` indexes the factory tree. The step signs `repomd.xml` with a detached signature. The job pushes the same tree as the pinned OCI image.

## How to (re)publish

Dispatch `Publish build-chain RPMs` with `cells=gnome50-el10-x86_64`, `dry_run=false`.
The build job builds the chain in the CentOS Stream 10 mock root (it resumes the banked partial when the action key matches).
The publish job does the steps above, and the run summary prints the image digest.
The `verify` check asserts that every published package resolves from the served index.

For installed systems the live endpoint stays [repo.tunaos.org](https://repo.tunaos.org); the OCI digest is the image build input.
