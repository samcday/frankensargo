# Grey-goo storage and boot contract

This document is the safety contract for frankensargo. It describes the
intended format and transaction semantics; it is not an assertion that the
target-side writer is already complete.

## Trust and identity

One physical sargo and one outer VG form the trust boundary. Before any target
write, the controller must match all of:

- `product=sargo` and the explicitly supplied fastboot serial;
- eMMC CID and GPT disk GUID;
- partition name, type GUID, PARTUUID, start LBA, and length;
- the captured inventory hash and current full donor hash; and
- the exact VG UUID, allowed PV UUID set, and manifest generation.

Kernel enumerations such as `mmcblk0p68` are observations, never identity.
Outer GPT names, types, geometry, and PARTUUIDs remain unchanged unless a later
hardware test demonstrates that changing a type GUID is required. The LVM PV
UUID and label inside an authorized donor are its takeover marker.

Frankensargo's primary and backup GPT headers both carry the all-zero disk
GUID. It remains part of the captured tuple, but contributes no uniqueness.
No live executor may match this device or authorize a donor from the disk GUID
alone: product, explicit fastboot serial, eMMC CID, full partition identity,
and inventory hash are a conjunction. A `partition_id` with the zero parent
GUID is meaningful only inside that already-verified device scope.

The CRC-valid backup GPT header on frankensargo points its entry-array LBA to
the primary table at LBA 2. There is no independent backup partition-entry
array. This layout is recorded rather than silently repaired; a raw off-device
GPT capture and tested restore path are prerequisites to the first write.
`bin/inventory-pocketboot` validates both headers, the shared entry-array CRC,
every entry's geometry, and the kernel view, then emits the snapshot hash used
by a future real manifest builder.

The Android ABL-facing `boot_a` and `boot_b` partitions are never PV
candidates. Neither are `dtbo`, `vbmeta`, modem/radio, persist, metadata, misc,
GPT structures, or any partition consumed directly by earlier firmware.

## Canonical layout

`userdata` becomes the initial, anchor PV after its own explicit bootstrap
authorization. It contains every critical LV and the thin-pool metadata:

| Role | LVM tag | Allocation |
|---|---|---|
| transaction store | `pocketboot.meta.v1` | thick, userdata only |
| rescue/distro boot filesystem | `pocketboot.bootfs.v1` | thick, userdata only |
| nested distro disk | `pocketboot.disk.v1` | normally thin |
| shared home filesystem | `greygoo.critical` | thick, userdata only |
| homed signing/record state | `greygoo.critical` | thick, userdata only |
| thin metadata and pmspare | `greygoo.critical` | thick, userdata only |
| Android logical artifact | `pocketboot.android.logical.v1` | thin, read-only |
| sparse raw donor archive | `greygoo.archive.v1` | thin, read-only |

The VG itself carries `pocketboot.vg.v1`. Tags are discovery hints; immutable
VG, PV, and LV UUIDs in the manifest are identity. Human-readable names such as
`ggmeta`, `boot-rescue`, `disk-mobian`, and `arc-4f2e8c0d91ab` are similarly not
identity.

Every allocation command names its permitted PV explicitly. It is followed by
an `lvs -a -o devices` placement assertion. Thin metadata, pmspare, `ggmeta`,
boot-rescue, shared home, and homed state may never migrate onto a reclaimed
system container.

## PocketBoot discovery contract

Normal PocketBoot must not relax its general exclusion of device-mapper and
loop devices. Guest disks can contain their own LVM signatures.

The manifest naming all allowed PVs lives inside the VG, so discovery is
deliberately two-stage. A provisioned capsule is bound once to the userdata
anchor PARTUUID and exact VG UUID (for example through generated boot-image
configuration). Adding later donors does not change that binding.

The manifest repeats that capsule binding and records every currently admitted
physical device as a `partition_id`/PARTUUID/PV UUID tuple. The redundancy is
intentional: host validation rejects a capsule/VG mismatch, a tuple for a
different GPT disk, duplicate identities, a candidate already present in the
allowlist, or a non-anchor tuple without a matching fenced/member lifecycle
record. `target/plan-discovery` renders the deterministic anchor-only and full
allowlist stages without opening a block device.

1. Resolve only the physical anchor by PARTUUID and give only that device to
   LVM.
2. From the incomplete VG, activate the userdata-pinned `pocketboot.meta.v1`
   LV read-only in partial mode; no other LV is eligible in this stage.
3. Read and validate the highest complete manifest generation, including its
   device fingerprint, anchor PV UUID, and expected VG UUID.
4. Resolve every manifest-approved PARTUUID and confirm its observed PV UUID,
   building the complete outer-LVM devices allowlist.
5. Rescan the exact VG and activate only manifest-approved boot candidates
   read-only.
6. Mount each `pocketboot.bootfs.v1` filesystem and scan it directly.
7. Treat each `pocketboot.disk.v1` LV as an explicit disk candidate, parse its
   nested GPT, and scan only its ESP/XBOOTLDR/Linux boot filesystems.

If the anchor-only metadata LV cannot be activated, PocketBoot stays in
recovery and never falls back to scanning arbitrary `dm-*`, loop, or physical
partitions for a similarly named VG. The exact partial-activation behavior is
part of the host LVM lab and frankensargo test matrix.

For LVM2 specifically, `lvs --readonly` is suitable for metadata reports, but
`lvchange --activate y --readonly` is not: LVM's global `--readonly` mode makes
no device-mapper calls, as documented by
[`lvchange(8)`](https://man7.org/linux/man-pages/man8/lvchange.8.html).
Temporary read-only activation instead uses exact candidate selection plus
`activation/read_only_volume_list = [ "@pocketboot.meta.v1",
"@pocketboot.bootfs.v1", "@pocketboot.disk.v1" ]`, followed by a hard check
that every resulting `/sys/block/dm-*/ro` is `1`. Persistently changing LV
permission with `lvchange -p r` during discovery would itself mutate VG
metadata and is forbidden.

This proves the exposed candidate is read-only, not that activation of a thin
candidate is physically write-free: its thin-pool dependency may update thin
metadata. PocketBoot records pool health/transaction IDs around activation and
must never describe this as forensic read-only access. Thick rescue bootfs is
the path that avoids that dependency.

The minimum resilient set is a thick `boot-rescue` LV plus one PocketBoot
capsule. A corrupt outer VG cannot be the only recovery route: the capsule
still contains its console, ADB, userspace fastboot, pstore support, and enough
takeover tooling to inspect/recover the VG.

### UKI behavior

PocketBoot's existing PE, arm64 Image, gzip/zboot, RAM-placement, DT repair,
and kexec code remains the loader foundation. Proper UKI support adds:

- Type #2 discovery only at `$BOOT/EFI/Linux/*.efi`;
- `.linux` as the sole required section;
- optional `.initrd`, `.cmdline`, `.osrel`, `.uname`, and `.dtb`;
- repeated `.dtbauto`, selecting the first image whose first compatible string
  matches the live firmware DT, overriding `.dtb`; and
- ordered multi-profile `.profile` sections with base inheritance and
  profile-specific overrides.

Embedded text accepts conventional trailing NUL/alignment padding but rejects
interior NULs. PE section length is the logical `min(VirtualSize,
SizeOfRawData)`, not alignment-padded raw length. Type #1 `uki`/`efi` references
can use the same payload path.

This is UKI **container extraction and kexec**, not UEFI Secure Boot,
Authenticode verification, TPM measurement, or systemd-stub policy emulation.
Those properties must not be implied by the UI or documentation.

The relevant upstream contracts are the [Boot Loader
Specification](https://uapi-group.org/specifications/specs/boot_loader_specification/)
and the [Unified Kernel Image
specification](https://uapi-group.org/specifications/specs/unified_kernel_image/).

## Normal and takeover profiles

The normal profile's code invokes only read-only LVM activation/reporting,
tagged boot discovery, UKI/BLS loading, and recovery interfaces. It contains no
automatic PV/VG/LV creation or extension workflow.

The transient takeover profile additionally contains:

- LVM mutation and metadata-backup tools;
- Android LP metadata parsing/mapping and logical-artifact extraction;
- resumable sparse copy plus full-stream hashing;
- firmware extraction/verification support;
- a durable manifest/journal controller; and
- an explicit, exact authorization UI.

There is one controller lock, one donor transaction, and one VG mutation at a
time. A normal boot never resumes an absorption transaction implicitly.

This compile/profile split prevents accidents; it is not a privilege boundary.
PocketBoot's lab images deliberately expose unauthenticated root UART, ADB, and
userspace-fastboot controls, and the canonical `lvm.static` binary necessarily
contains mutation subcommands. A cryptographically enforced takeover policy
would also have to gate those debug surfaces, which is outside this lab's
initial threat model.

## Monotonic donor lifecycle

```text
discovered
  -> copying
  -> archive-verified
  -> ready
  -> authorized
  -> absorb-intent
  -> pv-present
  -> vg-member-fenced
  -> capacity-released
  -> absorbed
```

Any identity, hash, LVM, placement, or thin-metadata discrepancy enters
`quarantined`. No state moves backward after the first donor write. What looks
like rollback is a new, separately authorized restore transaction.

- **discovered**: a read-only inventory and source-extent graph exist.
- **copying**: resumable imports are incomplete; the donor remains untouched.
- **archive-verified**: source and complete logical destination hashes match.
- **ready**: an off-device raw copy is verified, every touching extent is
  archived, required firmware is load-tested, and capacity gain is positive.
- **authorized**: one exact plan and confirmation nonce have been accepted.
- **absorb-intent**: the journal, manifest generation, and LVM backup are
  durable; forward recovery of this operation is permitted.
- **pv-present**: the exact planned PV UUID exists on the donor.
- **vg-member-fenced**: it is in the expected VG with allocation disabled.
- **capacity-released**: a reboot/recovery check passed and allocation is
  explicitly enabled.
- **absorbed**: the specified pool/LV extension and placement checks passed.

An authorization record binds an operation UUID, device fingerprint, donor GPT
identity and geometry, full raw SHA-256, archive/off-device-copy IDs, planned PV
UUID, current manifest hash/generation, requested allocation action, nonce,
confirmation method, and optional host signature. It is one-shot and cannot be
interpreted as permission to absorb a different donor.

## Transaction truth and persistence

The manifest is a request/history record, not sufficient evidence. Restart
reconciliation observes the actual PV UUID, VG UUID/sequence, LV UUIDs, thin
transaction ID, allocation state, placement, and hashes before advancing.

Canonical manifest snapshots live in two alternating files on `ggmeta`, each
hash-linked to the previous generation. A commit uses temporary file, file
`fsync`, rename, directory `fsync`, and a block flush. The operation journal,
authorization records, sparse-copy chunk maps, exact LP metadata bytes, and
`vgcfgbackup` snapshots live beside it and have another verified off-device
copy.

Bootstrap is the one interval before `ggmeta` exists. Its host-side plan
therefore records the future device UUID, userdata identity/hash, planned PV
and VG UUIDs, and confirmation. If power fails after `pvcreate`, LVM's observed
planned UUID determines whether to continue; the writer never blindly repeats
`pvcreate`.

### Restart reconciliation

| Observed fact | Permitted action |
|---|---|
| incomplete staging map, donor hash intact | resume/recreate staging only |
| destination complete but unverified | hash the entire destination |
| `absorb-intent`, donor wholly intact | resume that operation or cancel |
| donor partly erased, no valid PV | match exact intent/archive, continue forward |
| planned PV UUID exists and is orphaned | `vgextend`; never `pvcreate` again |
| planned PV already in expected VG | adopt observed state and fence it |
| VG metadata copies disagree | quarantine; reconcile consistent sequence |
| thin-data exceeds recorded size | health-check metadata, then adopt reality |
| unexpected signature, geometry, or UUID | quarantine for manual recovery |

## Bootstrap sequence

The userdata transaction is generated and retained on the host before any
write. Preconditions include a repeatable transient PocketBoot route, captured
stock recovery material, and a tested SysRq/fastboot escape path.

After exact authorization, takeover creates the planned userdata PV and VG,
then thick `ggmeta` and `boot-rescue` LVs. Thin metadata and pmspare are sized
for the projected final pool and maximum thin count, but thin-data starts
small. Substantial userdata space remains unallocated as recovery headroom.
Normal PocketBoot must cold-boot from the inactive Android boot slot and find
the LVM bootfs before any other physical partition becomes eligible.

The legacy nested-MBR planner and dracut mapper in this repository remain
useful for compatibility experiments with old PocketBoot. They are not the
canonical final layout and must not be used as authority for the whole-PV
userdata bootstrap.

## Slurping Android factory artifacts

Sargo uses retrofit dynamic partitions: physical `system_a` and `system_b`
containers hold LP metadata and extents for logical `system`, `vendor`, and
other artifacts. The controller must read every metadata slot from both
containers, validate bounds/overlaps, retain the exact metadata regions, and
build a reverse graph from each physical donor to every logical artifact that
touches it.

For each candidate while it is still read-only:

1. create a same-sized thin archive LV in already-owned space;
2. copy resumably, leaving only independently verified all-zero chunks
   unmapped;
3. hash the full source byte stream and the full logical destination byte
   stream, including holes-as-zero;
4. import useful expanded logical partitions into separately hashed,
   read-only thin LVs; and
5. copy the raw archive and restore metadata off-device and verify it again.

For an LP container, `ready` additionally binds hashes of the exact LP metadata
and reverse extent graph. The authorization planner requires explicit records
that all touching extents are archived and that maps reconstructed from the
archive expose the same bytes as maps reconstructed from the donor.

A local thick raw archive returns essentially no capacity. Even a sparse thin
archive is not a backup and consumes real mapped blocks. Before authorization:

```text
net_gain = donor_bytes
         - new_retained_artifact_mapped_bytes
         - projected_LVM_and_thin_metadata_growth
         - mandatory_free_space_reserve
```

The planner refuses a non-positive result and displays every term.

Android dynamic partitions are userspace device-mapper maps; the physical
containers must be reclaimed exactly once and overlapping live logical maps
must never be offered to outer LVM. See the [AOSP dynamic-partition
guide](https://source.android.com/docs/core/ota/dynamic_partitions/implement).

## Firmware gate

The archived logical `vendor` and physical modem sources contain firmware for
GPU, DSP, audio, Wi-Fi, modem, and sensors. `droid-juicer`'s
`configs/google,sargo.toml` is the starting extraction manifest.

The gate records extractor/config commits, every file hash, a bundle hash, and
a verified off-device copy. It becomes satisfied only after the intended
kernel cold-boots and logs successful loading/operation of those subsystems.
A factory-image download alone does not satisfy the gate.

## Absorbing one donor

Immediately before the first write, takeover rechecks identity, full donor
hash, mounts, swap, and device holders. It then persists `absorb-intent` and an
LVM metadata backup and flushes both.

The authorized writer invalidates the recorded LP/signature regions, creates
the exact planned random PV UUID, extends the exact VG, and fences the PV
non-allocatable. It tags the PV, saves/exports fresh VG metadata, then reboots.
Only after normal PocketBoot and takeover recovery both rediscover the complete
VG may a new authorization release the PV for allocation.

The requested `lvextend` names only that PV and normally extends thin-data.
Placement assertions prove that critical LVs and thin metadata did not move.
The controller then records the observed thin transaction ID and final state.

## Thin-pool policy

- Choose and record chunk size once.
- Size metadata up front with `thin_metadata_size` for final projected data and
  thin-LV count; keep a userdata-resident pmspare.
- Maintain a hard physical-headroom floor.
- Do not rely on LVM auto-extension, which might select the wrong PV.
- Never thin-provision transaction metadata, rescue bootfs, homed signing
  state, or irreplaceable shared homes.
- Do not casually `pvmove` thin-data. In particular, never move an archive's
  mapped extents onto the physical donor from which it was created.

Thin-pool corruption couples all thin distro images, so pool health and
metadata backups are part of every normal-boot status display. Replaceable
roots remain rebuildable; irreplaceable data remains thick and off-device.

## Restore boundary

Before `absorb-intent`, cancelling is cheap. Afterwards, restoration needs a
new exact authorization and enough staging space to:

1. fence the donor PV;
2. evacuate all allocated extents;
3. prove `pv_used=0`;
4. remove it from the VG and remove its PV label;
5. restore the raw archive byte-for-byte;
6. verify the complete hash and LP metadata; and
7. recreate and compare the logical mappings.

A conventional Android factory flash is not a safe substitute for this
transaction once a physical container belongs to the VG.

## Distro and home adapters

### PocketFed

Install its ext4 bootc/OSTree sysroot into a direct LV. Its initramfs needs the
exact outer-LVM devices allowlist and required device-mapper/LVM components.
Publish versioned UKI/BLS assets to a tagged bootfs LV. Disable
`aboot-deploy`, because an OSTree update must not overwrite a PocketBoot
capsule. Enable `pam_systemd_home` through an appropriate authselect profile.

### Mobian, Duranium, and BengalOS

Store their raw images in `pocketboot.disk.v1` thin LVs. PocketBoot scans the
nested GPT boot partition directly. After kexec, early userspace activates the
outer VG, exposes the selected LV as a partition-scanned disk, and lets the
guest's native root/verity/repart logic continue.

Duranium's current postmarketOS systemd build uses `-Dhomed=disabled
-Duserdb=false`; it needs a rebuilt package and a bypass for its `useradd`-based
first boot. BengalOS is design input until its immutable phone target supports
sargo and a complete updater.

### Shared encrypted homes

The thick `home` LV is a filesystem containing systemd-homed LUKS2
`name.home` images. Every participating OS mounts the thick `homed-state` LV at
`/var/lib/systemd/home` before `systemd-homed.service`, uses one fixed human
UID/GID policy, and enables compatible PAM/userdb integration.

The record signing keys live outside the individual LUKS images, hence the
shared state LV. All participating roots remain equally trusted: any root can
replace PAM and capture credentials. See [homectl](https://www.freedesktop.org/software/systemd/man/latest/homectl.html)
and [systemd-homed.service](https://www.freedesktop.org/software/systemd/man/latest/systemd-homed.service.html).

## Source state at project creation (2026-07-10)

- PocketBoot was pinned to the remote head containing the SDM670 Type-C/USB
  fixes; the local sibling checkout was two commits behind.
- The known-working local sdm670 beta4 kernel was 125 commits behind its cached
  upstream tracking branch. Hardware validation gates any pin change.
- PocketFed, Duranium, and BengalOS matched their audited remote heads.
- pmaports, Fastboop, and blob-wrangler had newer pinned remote heads than the
  corresponding local checkouts.
- `~/src/pocketblue/pocketblue` has user changes and an untracked sargo device
  tree. It is reference material and is not mutated here.
- `~/src/lk2nd2nd` contains the hardware-proven UKI experiment. Its useful
  inheritance is the section map and proof of life; PocketBoot's safer loader
  is the implementation base.
