# Duranium outer-LVM initrd adapter

`bin/build-duranium-lvm-adapter` builds a deterministic, uncompressed `newc`
initrd component for one exact Duranium thin disk LV. PocketBoot appends it
after the UKI's embedded initrd. The component discovers only the userdata
anchor PV, verifies the PV, VG, and LV identities, activates only the named
thin LV (plus its unavoidable thin-pool dependencies), and exposes it through
a read-only loop whose kernel reference is `rootdisk`.

This is an anchor-only bootstrap adapter. It deliberately cannot discover
additional PVs. Keep the Duranium thin LV and its pool data reachable through
the userdata anchor until a later manifest-bound adapter explicitly supports
more PV identities.

## Published artifact audit

The audited release is `google-sargo_phosh_edge_26070701`:

- compressed raw image: 1,093,164,684 bytes, SHA-256
  `035fa4b4f1ea70f6d2706f7e0d60e4c3f97d36f7571de739fbb29eab02f99f68`;
- raw disk: 6,862,950,400 bytes, GPT disk UUID
  `eb5e7a01-f599-4065-b5c8-4f715b6a6d39`;
- partition 1: 1 GiB FAT32 ESP beginning at LBA 2048;
- partition 2: 400 MiB ARM64 `/usr` Verity partition;
- partition 3: 5 GiB ARM64 `/usr` partition;
- UKI: `EFI/Linux/google-sargo_phosh_edge_26070701.efi`, 103,735,296
  bytes, SHA-256
  `eedefda43cb97ced8d4be0b6a50c1354cbb840ac798c3d795dabb6972e213757`.

The ESP's `loader/entries` directory is empty. `loader/loader.conf` contains
only commented timeout and console settings. The release therefore has one
direct Type #2 UKI and **no BLS entry**.

The UKI embeds this command line:

```text
quiet splash plymouth.ignore-serial-consoles plymouth.prefer-fbcon gnome.initial-setup=0 usrhash=a82ab03b602065101dcf0c47fd709bc03f2e2edd47dd82897cff0ed539ecf627 rw
```

Its initrd runs systemd 261.1 and has `/init` as a symlink to
`/usr/lib/systemd/systemd`. It contains `systemd-gpt-auto-generator`,
`systemd-dissect`, `systemd-repart`, `systemd-loop@.service`, `losetup`,
`blkid`, `blockdev`, `dmsetup`, udev, and the required kernel modules. It does
not contain LVM userspace or `thin_check`.

Systemd executes all generators in parallel. A conventional generated service
would therefore create the loop too late for `systemd-gpt-auto-generator`.
The appended component deliberately replaces the `/init` symlink with a small
shim. The shim mounts the initrd API filesystems, performs the exact attach,
and only then execs systemd. A generator also makes a verification service a
hard requirement of `initrd-root-device.target` and `initrd-usr-fs.target`;
that service runs before root mounting and `systemd-repart.service`.

## Build the component

The builder requires caller-supplied SHA-256 pins. The dynamic `thin_check`
closure is kept below `/usr/lib/frankensargo-duranium`; it does not replace
Duranium's newer musl loader or libudev. These hashes are for the PocketBoot
inputs currently cached from its pinned Alpine packages:

```sh
cd /home/deck/src/frankensargo

# Obtain these three LVM identifiers from the authorized bootstrap result.
# Do not substitute names, guessed values, or an unverified `lvs` report.
PV_UUID='REPLACE-WITH-EXACT-LVM-PV-UUID'
VG_UUID='REPLACE-WITH-EXACT-LVM-VG-UUID'
DISK_LV_UUID='REPLACE-WITH-EXACT-LVM-LV-UUID'

SOURCE_DATE_EPOCH=0 bin/build-duranium-lvm-adapter \
  --userdata-partuuid db04e713-11c3-4d68-bec2-8cc483bd3891 \
  --pv-uuid "$PV_UUID" \
  --vg-uuid "$VG_UUID" \
  --disk-lv-uuid "$DISK_LV_UUID" \
  --disk-lv-name disk-duranium \
  --disk-lv-tag pocketboot.disk.v1 \
  --lvm-static .work/pocketboot/target/lvm2/aarch64-unknown-linux-musl/lvm.static \
  --lvm-static-sha256 b83d704df60ca281deb56f1704d74db731a05365e90d0162556b2c355b572d39 \
  --thin-check .work/pocketboot/target/thin-tools/aarch64-unknown-linux-musl/pdata_tools \
  --thin-check-sha256 1f7a35217810ddef749508713c1e31fdaee65f182ecab5ee374ed2afe83b19a2 \
  --thin-loader .work/pocketboot/target/thin-tools/aarch64-unknown-linux-musl/loader \
  --thin-loader-sha256 fc61b6e11fd8c6bfee3235865249cd78eea34aa36d88277e6008038031160993 \
  --thin-libgcc .work/pocketboot/target/thin-tools/aarch64-unknown-linux-musl/libgcc \
  --thin-libgcc-sha256 fd240181fecbb70409aa70ccb76671a96ac22be540e6398fe66d6800a16b2b18 \
  --thin-libudev .work/pocketboot/target/thin-tools/aarch64-unknown-linux-musl/libudev \
  --thin-libudev-sha256 83fe88a925f9c43ef8d185b6f1baec5bd6b02455e02b2c558302f6d18d0ad77f \
  --output out/duranium/frankensargo-duranium-lvm-26070701.cpio
```

The tool rejects noncanonical identities, a hash mismatch, non-AArch64 ELF
inputs, a non-static `lvm.static`, a dynamic `thin_check` without its complete
pinned runtime, and an existing output unless `--force` is explicit. Archive
entries have sorted paths, root ownership, fixed modes, and the selected
`SOURCE_DATE_EPOCH`; no host path or source mtime enters the archive.

The exact LVM executable inside the component is
`/usr/lib/frankensargo-duranium/lvm.static` (LVM 2.03.35 for the pins above).
The boot script invokes it as `lvm.static <subcommand> --devices <exact-anchor>
--nohints --quiet ...`; it never depends on a host or Duranium `/usr/sbin/lvm`.

Run the focused offline tests inside the canonical Fedora container:

```sh
distrobox enter --name fedora-latest -- bash -lc '
  export PATH="$HOME/.cargo/bin:$PATH"
  cd /home/deck/src/frankensargo
  tests/test-duranium-lvm-adapter.sh
'
```

## Add the missing BLS entry

PocketBoot's direct Type #2 scan cannot associate an external initrd with the
published UKI. Its patched BLS loader recognizes the standard Type #1 `uki`
key and ordered `initrd` lines; it intentionally does not treat a generic
`efi` payload as a kernel UKI.
Therefore stage the adapter and add this exact entry while the imported disk LV
is still writable and untagged:

```text
# /loader/entries/frankensargo-duranium.conf
title Duranium 26070701 (frankensargo LVM)
version 26070701
architecture aa64
uki /EFI/Linux/google-sargo_phosh_edge_26070701.efi
profile 0
initrd /EFI/Linux/frankensargo-duranium-lvm-26070701.cpio
options quiet splash plymouth.ignore-serial-consoles plymouth.prefer-fbcon gnome.initial-setup=0 usrhash=a82ab03b602065101dcf0c47fd709bc03f2e2edd47dd82897cff0ed539ecf627 rw root=dissect mount.usr=dissect sysrq_always_enabled=1
```

A nonempty BLS `options` line replaces the UKI's embedded command line. The
entry must therefore repeat the complete published command line and then add
`root=dissect mount.usr=dissect sysrq_always_enabled=1`; supplying only those
new options silently drops `usrhash` and must not be accepted. The SysRq
argument keeps emergency serial recovery enabled in the downstream Duranium
kernel rather than only in PocketBoot itself; it still requires that kernel to
have Magic SysRq support built in.

Use this exact on-ESP component path:

```text
/EFI/Linux/frankensargo-duranium-lvm-26070701.cpio
```

Finally add this active line to `/loader/loader.conf`, preserving its existing
comments:

```text
default frankensargo-duranium.conf
```

That makes the BLS entry preferred ahead of the duplicate direct Type #2 UKI.
This preference depends on
`patches/pocketboot/0007-read-loader-conf-default.patch`; its parser and
ordering have offline unit coverage, but the Duranium selection remains
hardware-unproved until the prepared disk is scanned by that exact PocketBoot
build.
Only after the component, BLS entry, `loader.conf`, and their hashes have been
verified should the ESP be unmounted, the staging loop detached, and the LV
receive `pocketboot.disk.v1`/read-only exposure.

For a mounted staging ESP at `$ESP`, a deterministic copy check is:

```sh
adapter=out/duranium/frankensargo-duranium-lvm-26070701.cpio
installed=$ESP/EFI/Linux/frankensargo-duranium-lvm-26070701.cpio
test -f "$ESP/loader/entries/frankensargo-duranium.conf"
grep -Fx 'default frankensargo-duranium.conf' "$ESP/loader/loader.conf"
grep -Fx 'initrd /EFI/Linux/frankensargo-duranium-lvm-26070701.cpio' \
  "$ESP/loader/entries/frankensargo-duranium.conf"
test "$(sha256sum "$adapter" | cut -d ' ' -f 1)" = \
  "$(sha256sum "$installed" | cut -d ' ' -f 1)"
```

To prove the builder itself is reproducible, build twice with identical
arguments and different output names, then run `cmp` and `sha256sum`:

```sh
cmp out/duranium/adapter-a.cpio out/duranium/adapter-b.cpio
sha256sum out/duranium/adapter-a.cpio out/duranium/adapter-b.cpio
```

## Runtime failure boundary

Before systemd starts, the shim requires canonical `root=dissect`,
`mount.usr=dissect`, `sysrq_always_enabled=1`, and `usrhash=` arguments; finds
exactly one sysfs partition with the configured PARTUUID; rejects removable or
ambiguous matches; and verifies every bundled tool hash. LVM scanning is
restricted by both `--devices` and an accept-two/reject-all filter for the
resolved anchor and its exact PARTUUID link.

The script requires exact PV, VG, and LV UUIDs, exact LV name, the exact role
tag, thin-LV attributes, and a thin-pool dependency. It runs only an exact
`lvchange --activate y VG/disk-duranium`; no `vgchange -ay`, tag-wide
activation, or candidate scan exists. LVM configuration disables event and
auto activation, makes the role tag read-only, and runs `thin_check -q` through
the isolated runtime. The resulting LV is additionally marked read-only with
`blockdev` and attached using:

```text
losetup --find --show --nooverlap --read-only --partscan --loop-ref rootdisk LV
```

The script then verifies the device-mapper UUID, read-only flags, backing
device, GPT, and partition appearance. Any mismatch enters the initrd
`emergency.target`; it never falls back to another PV, VG, LV, or loop. Thin
pool activation can still update thin metadata as part of normal LVM operation
and is not claimed to be physically write-free.
