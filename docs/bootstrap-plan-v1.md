# Userdata anchor bootstrap plan v1

`target/plan-bootstrap` is the last host-only gate before a future executor is
allowed to turn frankensargo's `userdata` partition into the first LVM PV. It
reads and hashes ordinary files, validates a completed PBREAD1 run, and prints
one deterministic JSON plan. It does not enumerate a phone, open a block
device, or execute any argv that it emits.

The output schema is
[`bootstrap-plan-v1.schema.json`](../schema/bootstrap-plan-v1.schema.json).
[`bootstrap-plan-v1.example.json`](../examples/bootstrap-plan-v1.example.json)
is synthetic and demonstrates the complete structure; none of its UUIDs or
hashes authorize work on frankensargo.

## Inputs and binding

A plan requires the canonical live inventory, a completed PBREAD1 backup run,
the exact transient PocketBoot image used for that run, an explicit serial and
PARTUUID, a new operation UUID, and a pre-generated LVM PV UUID:

```sh
target/plan-bootstrap \
  --inventory out/inventory/frankensargo.json \
  --pbread-run /home/deck/frankensargo-backup/2026-07-12/pbread1-userdata \
  --pocketboot-image out/pocketboot/pocketboot-sargo-lab.img \
  --serial 99NAY1AZG1 \
  --partuuid db04e713-11c3-4d68-bec2-8cc483bd3891 \
  --operation-uuid NEW-RFC4122-V4-UUID \
  --planned-pv-uuid NEW-LVM-PV-UUID \
  >bootstrap-plan.json
```

Redirecting stdout writes only a host file. The planner itself has no command
execution path.

Before emitting a plan it independently checks:

- inventory canonical hash and the exact inventory file hash;
- `sargo`, `google,sargo`, explicit fastboot serial and eMMC CID;
- the observed all-zero GPT disk GUID, primary/backup header hashes, aliased
  backup-entry layout and entry-array hash;
- exactly one `userdata` entry selected by the supplied PARTUUID, observed as
  the fixed sargo target `mmcblk0p72`;
- GPT type, start LBA, sector count, 512-byte sector size and exact
  53,648,801,280-byte geometry;
- the PBREAD1 manifest checksum and its complete identity/geometry binding;
- the PBREAD1 journal file hash and `source-matched` terminal state;
- a fresh host read of every PBREAD1 chunk and the assembled raw image through
  `pbread1.verify_run`;
- equality of the journal's complete source hash and destination hash; and
- the supplied PocketBoot image's byte size and SHA-256 against the PBREAD1
  manifest.

The inventory and PocketBoot image are rejected if their file identity, size,
or timestamps change across parsing and hashing. PBREAD verification, journal
parsing and journal hashing are held under PBREAD's shared run lock, so a
cooperating backup writer cannot splice two journal states into one plan.

The output repeats those facts and binds them into
`authorization_sha256`. The confirmation token is derived from the operation
UUID and that hash. A future executor must still repeat the live identity,
geometry, mount, swap and holder checks immediately before the first write.
PocketBoot has no `/dev/disk/by-partuuid`, so device resolution is an exact
sysfs PARTUUID scan; the complete live-source check uses PBREAD1's bounded OEM
hash operation, not a nonexistent initrd `sha256sum`. A plan is evidence, not
proof that the phone remained unchanged after PBREAD1 finished.

The recovery check is explicitly a manual, out-of-band operator attestation;
this JSON cannot infer a successful ABL fastboot or UART/SysRq recovery drill.
`transaction.runtime_artifacts` binds the executor's two runtime dependencies:

- `/sbin/lvm.static`: LVM 2.03.35, 2,309,032 bytes, SHA-256
  `b83d704df60ca281deb56f1704d74db731a05365e90d0162556b2c355b572d39`;
- `/etc/lvm/lvm.conf`: 432 bytes, SHA-256
  `16eb1787836608cfaff40aa904705b2138928010b1b4011e4ab981b4d43e2998`.

Before writing, an executor must pull both running files, verify exact path,
size and hash, and require `lvm.static version` to report 2.03.35. PocketBoot
still has no whole-image build-identity getvar, so these exact dependencies
narrow rather than eliminate the runtime-attestation gap.

## Exact initial layout

All physical sizes are multiples of the 4 MiB VG extent size. Every thick LV,
the thin metadata LV, the LVM-managed metadata spare and the initial thin-data
LV are constrained to the resolved userdata PV. No default allocator is
authority.

| LV | Kind | Physical size | Purpose |
|---|---|---:|---|
| `ggmeta` | thick, critical | 512 MiB | transaction records, manifests and VG backups |
| `boot-rescue` | thick, critical | 2 GiB | rescue UKIs and boot artifacts |
| `home` | thick, critical | 8 GiB | shared systemd-homed image backing store |
| `homed-state` | thick, critical | 256 MiB | shared homed records and signing material |
| `pool-meta` | thin metadata, critical | 512 MiB | deliberately oversized for later goo growth |
| `lvol0_pmspare` | LVM-managed, critical | 512 MiB | thin metadata repair spare |
| `pool` | thin data | 20 GiB | initial replaceable distro/artifact data |
| `disk-duranium` | thin | 20 GiB virtual | complete Duranium GPT disk target |

The physical LVs total 34,091,302,912 bytes (31.75 GiB). A conservative 64 MiB
budget covers two 16 MiB PV metadata areas and the 1 MiB data alignment. The
remaining capacity is rounded down to complete 4 MiB extents, leaving a
3,653,120-byte non-extent tail and 19,486,736,384 bytes of conservative free
extents:

- 17,179,869,184 bytes (16 GiB) are a mandatory recovery reserve;
- 2,306,867,200 bytes remain as extra uncommitted extents.

The 20 GiB Duranium LV is virtual capacity, not a promise that it may consume
the whole thin pool. The published Duranium image is much smaller; import must
monitor mapped blocks and preserve thin-pool headroom. The pool uses 256 KiB
chunks, `errorwhenfull=y`, and `discards=nopassdown`. Automatic pool growth is
forbidden because a later multi-PV VG could otherwise place critical data on
the wrong donor.

The 512 MiB metadata size is intentionally extravagant for a 20 GiB initial
pool. It avoids moving thin metadata when the pool later grows across reclaimed
partitions. `lvconvert --poolmetadataspare=y` creates LVM's managed
`lvol0_pmspare`; the command names the userdata PV as its only allocation
candidate. The managed spare has no custom tag because LVM owns the hidden LV;
its exact initial device placement and pmspare role are the authority. Later
transactions must forbid `pvmove` or extension of critical LVs onto donor PVs;
the initial positional constraint cannot prevent a future privileged operator
from deliberately migrating extents.

## UUID policy

Only the PV UUID is accepted from the operator and passed to `pvcreate`. It
must be an independently generated LVM-format ID and is frozen in the plan
before the first write. `pvcreate` uses `--norestorefile` because this is a new
planned UUID rather than restoration from existing VG metadata.

The installed LVM CLI does not offer supported `vgcreate` or `lvcreate`
arguments for preselecting VG/LV UUIDs. The plan therefore requires LVM to
generate those IDs and requires the future executor to capture and fsync each
observed UUID before proceeding. The real VG UUID then becomes an input to the
capsule-bound PocketBoot rebuild; no guessed VG UUID appears in this plan.

## Inert command argv

`transaction.command_argv` contains arrays, never a shell string. Every LVM
argv is the PocketBoot runtime form `/sbin/lvm.static APPLET ...`; PocketBoot
does not install standalone `pvcreate`, `vgcreate`, or `lvcreate` symlinks.
Every command and report also includes `--devices
@USERDATA_BLOCK_DEVICE@`, `--nohints`, and the command-line override
`backup/backup=0 backup/archive=0`. Together these fence device discovery and
prevent implicit archives in PocketBoot's volatile `/etc/lvm`; reports add
`--readonly`. Commands which allocate physical extents name the same device
placeholder again as their positional PV. A future executor must resolve it
from the bound PARTUUID after all live gates pass and reject a non-identical
device.

Two state placeholders are deliberately distinct. Checkpoint JSON and pulled
VG metadata go below `@HOST_DURABLE_STATE_DIR@`. Explicit `vgcfgbackup` writes
first below `@POCKETBOOT_VOLATILE_STATE_DIR@`; the host executor must pull the
exact bytes, SHA-256 them, and fsync the destination before the next mutation.
The volatile phone path is never described as durable authority.

The argv sequence describes `pvcreate`, `vgcreate`, PV tagging, thick LV
creation, explicit thin metadata/data conversion, creation of the Duranium
thin LV, and a `vgcfgbackup`. Applet dispatch and option grammar are checked
against PocketBoot's built static LVM 2.03.35 under `qemu-aarch64-static`; the
same grammar and positional-PV form were cross-checked read-only against LVM
2.03.38 inside the Steam Deck's `fedora-latest` distrobox. No command was run
against frankensargo; merely printing the plan changes nothing.

Each command has an exact-before/exact-after checkpoint. The future executor
must run at most one command, capture the listed UUID/placement fields, write
an explicit read-only `vgcfgbackup` after every post-VG mutation, pull its
result, and fsync the numbered host state before considering the next mutation.
On restart it accepts an exact postcondition as completed; otherwise the exact
precondition must still hold. It must never blindly replay `pvcreate -ff` or an
LV creation command. This makes the stated LVM-generated VG/LV UUID policy part
of the machine-readable sequence rather than a prose-only promise.

`disk-duranium` starts writable and tagged `greygoo.import-pending`; it is not
visible to PocketBoot as a boot disk. Only after the whole image has been
imported and independently read/hash-verified does `post_import_argv` remove
the pending tag, make the LV read-only, and add `pocketboot.disk.v1`.

The plan deliberately does not choose filesystems, format LVs, import the
Duranium image, create a bound capsule, switch Android slots, or boot anything.
Those are later transactions with their own evidence and recovery points.
The thick and pre-conversion pool LVs are therefore created inactive with
`--zero n --wipesignatures n`; LVM 2.03.35 rejects `-an` combined with `-Zy` or
`-Wy`. Their later format/import transaction must initialize and verify them
before activation or use.
