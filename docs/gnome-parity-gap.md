# GNOME desktop parity gap — sailfin / flounder / grouper

Measured 2026-08-11 21:51:17 UTC by `scripts/measure-gnome-parity-gap.py` (tunaos-packages#132).
This doc includes two measurements: layer sizes on GHCR amd64, and lists of requested packages from desktop manifests in tunaOS compared with the GNOME contract.
Machine-readable result: `docs/gnome-parity-gap.json`.

Reproduce with:

```
scripts/measure-gnome-parity-gap.py
```

## 1. Image size deltas (GHCR, amd64, compressed)

| variant | base | gnome | delta | 2026-07-30 delta (issue) |
|---|---|---|---|---|
| sailfin | 1.52 | 1.96 | +0.44 | +0.16 |
| flounder | 1.14 | 1.75 | +0.61 | +0.33 |
| grouper | 1.87 | 2.32 | +0.45 | +0.32 |
| skipjack | 2.60 | 2.95 | +0.35 | +0.99 |
| albacore | 2.60 | 2.95 | +0.35 | +1.00 |
| yellowfin | 2.53 | 2.88 | +0.35 | +0.99 |
| bonito | 3.27 | 3.96 | +0.69 | +0.70 |
| marlin | 1.36 | 2.10 | +0.74 | +0.68 |
| guppy | 3.34 | 4.90 | +1.56 | +1.76 |

The build system rebuilt all nine editions on 2026-08-10.
Deltas for EL editions dropped from ~+1.0 GB to ~+0.35 GB after moving to native GNOME 50 RPMs.
Non-RPM editions rose to +0.44/+0.61/+0.45 GB after maintainers fixed the manifests.
Size alone no longer distinguishes healthy from thin editions.

## 2. GNOME contract coverage per variant

### Core (contract + session surface)

| variant | gdm | gnome-shell | mutter | gnome-session | gnome-keyring | gvfs | nautilus | portal-gnome | gnome-settings-daemon | gnome-control-center | portal-gtk |
|---|---|---|---|---|---|---|---|---|---|---|---|
| sailfin | ✔ | ◆ | ◆ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| flounder | ◆ | ◆ | ◆ | ◆ | ◆ | ◆ | ✔ | ✔ | ◆ | ◆ | ✔ |
| grouper | ◆ | ◆ | ◆ | ✔ | ✔ | ◆ | ✔ | ✔ | ◆ | ◆ | ✔ |
| skipjack | ✔ | ✔ | ◌ | ◌ | ◌ | ✔ | ✔ | ✔ | ✔ | ✔ | ◌ |
| albacore | ✔ | ✔ | ◌ | ◌ | ◌ | ✔ | ✔ | ✔ | ✔ | ✔ | ◌ |
| yellowfin | ✔ | ✔ | ◌ | ◌ | ◌ | ✔ | ✔ | ✔ | ✔ | ✔ | ◌ |
| bonito | ✔ | ✔ | ◌ | ◌ | ◌ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |

`✔` listed explicitly · `◆` supplied by a metapackage · `◌` supplied by an installed dnf group · `✘` missing from lists.

### Apps measured absent from the openSUSE pattern (2026-07-30)

| variant | gnome-bluetooth | gnome-online-accounts | gnome-initial-setup | gnome-disk-utility | fwupd | yelp | orca | search-index | gnome-color-manager | gnome-remote-desktop | gnome-user-docs |
|---|---|---|---|---|---|---|---|---|---|---|---|
| sailfin | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| flounder | ◆ | ◆ | ◆ | ✔ | ◆ | ◆ | ◆ | ◆ | ◆ | ◆ | ◆ |
| grouper | ◆ | ◆ | ◆ | ✔ | ◆ | ✔ | ◆ | ◆ | ◆ | ◆ | ✔ |

`search-index` is `tracker`/`tracker-miners` on dnf/apt and `tinysparql`/`localsearch` on openSUSE.

## 3. Findings

- **Requested lists cover the GNOME contract**: Each audited variant requests the core packages and apps missing from the initial audit.
- sailfin requests 62 names explicitly.
- flounder uses Debian's `gnome-core` metapackage and explicit portal packages.
- grouper uses `ubuntu-desktop-minimal` plus `gnome-keyring` packages.
- **Current images match the reference deltas**: Fixes to manifests landed in tunaOS on 2026-07-30/31 (ce11d21, d8ebdf6). The rebuilt editions match the EL reference ranges (+0.34–0.36 GB).
- **Remaining gap 1 — apt image inventories**: bootc does not commit `/var`, so `dpkg-query` returns empty on flounder and grouper. The build check is the active guard.
- **Remaining gap 2 — sailfin image inventory**: `rpm -qa` runs on `sailfin:gnome`, but no inventory file exists beside the image. Adding `rpm -qa` output to image metadata solves this gap.
- **Next candidates — zypper lists**: `kde.yaml` lists 3 zypper names, `xfce.yaml` 3, and `niri.yaml` 2. These pattern families need explicit component definitions (tunaos-packages#133).

## 4. Provenance

- `gnome.yaml`: `b354498ae888` (2026-08-11) — docs: add references to package source policy (#1319)
- `gnome-debian.yaml`: `28ac5e8ea9fe` (2026-08-07) — feat(flatpak): Flathub everywhere, curated preinstall set, and a contract that keeps both (#1062)

Registry: GHCR reachable at 2026-08-11 21:51:17 UTC.

## 5. Caveats

- Package counts are unreliable for apt bases because bootc omits `/var`. An installed inventory in `/usr` helps to compare published editions of apt directly.
- This audit reads manifests at their fetched commit. Changes show in the next measurement run.
- `sailfin:gnome` builds an ISO end-to-end (tuna-os/iso-builder#32). Its published image supports `rpm -qa` inventory collection.