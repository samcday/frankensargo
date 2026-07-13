# frankensargo

One Pixel 3a (`sargo`), several immutable Linuxes, and an expanding grey goo
made of LVM. The curse is intentional; the consent and recovery boundaries are
not.

The canonical design is now **LVM all the way up to Linux `/boot`**. Android's
`boot_a` and `boot_b` remain tiny ABL-facing PocketBoot capsules, or the same
takeover image can be launched transiently with `fastboot boot`. Everything
PocketBoot launches may live inside the owned VG:

```text
ABL
 ├── boot_a / boot_b                 PocketBoot capsules, outside LVM
 └── transient fastboot boot         takeover/recovery PocketBoot
                 │
                 ▼
       VG franken  [pocketboot.vg.v1]
         ├── ggmeta                  thick transaction metadata
         ├── boot-rescue             thick boot filesystem
         ├── boot-*                  tagged BLS/UKI filesystems
         ├── home                    thick shared *.home backing store
         ├── homed-state             thick homed identity/signing state
         ├── pool_tmeta + pmspare    thick, permanently on userdata
         └── thin pool
              ├── disk-mobian        complete nested GPT disk
              ├── disk-duranium      complete nested GPT disk
              ├── root-pocketfed     direct filesystem root
              ├── android-*          extracted logical artifacts
              └── arc-*              sparse local factory-image copies
```

The capsule binds the userdata-anchor PARTUUID and exact VG UUID. PocketBoot
first exposes only that PV; after reading the thick metadata LV it uses the
manifest's exact physical PARTUUID/PV UUID pairs. It then activates only
explicitly tagged LVs. A `pocketboot.bootfs.v1` LV is mounted and scanned
directly. A
`pocketboot.disk.v1` LV is treated as a disk, its nested GPT is parsed, and its
ESP/XBOOTLDR/boot filesystem is scanned. This admits ordinary Mobian and
Duranium disk images without flattening their native layouts.

## Expanding grey goo

The first explicitly destructive operation converts `userdata` into the
anchor PV. Later, the takeover profile absorbs one donor partition at a time:

```text
discovered -> copying -> archive-verified -> ready -> authorized
           -> absorb-intent -> pv-present -> vg-member-fenced
           -> capacity-released -> absorbed
```

Importing artifacts into already-owned LVM space is reversible and may be
automated. Writing the donor is a separate, one-shot authorization bound to the
phone identity, GPT identity, exact PARTUUID/start/size, full source hash,
archive object, planned PV UUID, requested capacity change, and manifest
generation.

Before `system_a` or `system_b` can reach `ready`, the process must:

1. inventory every Android logical-partition extent touching that container;
2. make and fully verify an off-device raw copy;
3. retain any wanted raw/logical artifacts as verified thin LVs;
4. extract and hash the sargo firmware bundle off-device; and
5. cold-boot the intended kernel and prove the firmware actually loads.

The manifest carries hashes of the raw LP metadata and reverse extent graph,
plus explicit assertions that every touching extent is archived and that
mappings reconstructed from the archive match the source.

The off-device raw copy is the rollback authority. A sparse on-device archive
is useful, but is not a backup. The authorization plan must report:

```text
net gain = donor bytes
         - newly allocated retained-artifact blocks
         - metadata growth
         - mandatory free-space reserve
```

No positive net gain means no takeover.

## Two PocketBoot personalities

- **normal** activates only allowlisted candidates and requires every exposed
  boot/disk device to be read-only, scans BLS plus Type #2 UKIs, launches the
  selected payload, and exposes recovery.
- **takeover** is preferably transient. It adds LVM mutation tools, Android LP
  mapping/import, resumable hashing/copying, the transaction journal, and the
  explicit authorization UI.

Normal mode never contains a generic "absorb everything" workflow. This is an
accident boundary, not a security boundary: the lab capsule deliberately has
unauthenticated root debug and fastboot surfaces, and canonical LVM userspace
contains mutation subcommands. After the first donor write, recovery is
forward-only unless a separately authorized restore transaction has enough
external staging space.

## Host-only work

The default validation and build workflow is phone-free:

```sh
make check
bin/source-status --remote
bin/build-pocketboot --prepare-only
bin/build-pocketboot
python3 lib/pocketboot_bundle.py verify --manifest \
  out/pocketboot/pocketboot-sargo-lab.img.bundle.json
```

The opt-in no-ACM control keeps the exact same 0001-through-current patch
tree and publishes under a separate directory and basename:

```sh
bin/build-pocketboot --no-acm --prepare-only
bin/build-pocketboot --no-acm
python3 lib/pocketboot_bundle.py verify --manifest \
  out/pocketboot-noacm/pocketboot-sargo-lab-noacm.img.bundle.json
```

It post-processes only the two Android-v2 cmdline fields of the unpublished
temporary image, requiring exactly one `pocketboot.acm` token and canonical
padding, and refuses a known AVB-footer-sealed artifact. Its provenance records
the unchanged base/effective source tree,
base/effective cmdlines, parent/result hashes, exact changed spans, and proof
that every non-cmdline byte is identical. The default build remains unchanged.
`build-pocketboot-bound --no-acm` composes the same profile with the observed
VG/PV binding inside its journaled, restored one-shot source edit.

Generic builds refuse every pre-existing final bundle path. All members are
hashed and fsynced before they are linked into place, and the hash-bound
`.bundle.json` completion manifest is published last. A killed build can leave
partial files but no completion manifest; consumers must require the manifest
verifier above rather than accepting a checksum sidecar by itself.

`make check` uses regular sparse files. `build-pocketboot` builds files only
and never runs fastboot; the full invocation writes the image, checksum, and
input/patch provenance under `out/pocketboot/`. Source revisions are pinned in
[`config/sources.lock`](config/sources.lock); the sdm670 beta4 kernel remains
pinned until a frankensargo UART, USB gadget, storage, and kexec smoke test says
otherwise.

The current PocketBoot patch is an intentionally interim bridge to the final
two-stage contract. It can bind discovery to exactly one
`pocketboot.vg_uuid=` plus repeated `pocketboot.pv_partuuid=` kernel arguments,
rejects removable, missing, duplicate, or ambiguous matches, and otherwise
leaves LVM discovery disabled. It does **not** yet read `ggmeta` or validate the
manifest's PARTUUID/PV-UUID tuples. The generic lab artifact therefore carries
no invented storage UUIDs; a capsule-binding generator comes only after the
frankensargo has a real anchor VG.

The stack also implements the raw, non-PTY subset of standard ADB `shell_v2`.
Bootstrap and Duranium import commands use one shared exact-serial host client
that proves fresh separated stdout/stderr and a typed exit frame, then rejects
legacy status zero and truncated streams on every argv. This support is in
patch `0013-adb-shell-v2-status.patch`; it has passed the host and PocketBoot
test suites but is not present in either previously staged hardware image.

The old nested-MBR userdata planner remains as compatibility/bootstrap
research, not the final layout:

```sh
target/plan-layout /path/to/a/userdata-sized-file
```

The architecture and boot contract are in [`DESIGN.md`](DESIGN.md); the exact
transaction protocol, role tags, manifest schema, and crash reconciliation are
in [`docs/grey-goo-protocol.md`](docs/grey-goo-protocol.md). The host-only
planner can exercise the synthetic ready-donor example without touching a
phone:

```sh
target/plan-discovery \
  --manifest examples/grey-goo-manifest-v1.example.json

target/plan-absorb \
  --manifest examples/grey-goo-manifest-v1.example.json \
  --partition-id gpt:11111111-2222-4333-8444-555555555555/66666666-7777-4888-9999-aaaaaaaaaaaa \
  --operation-uuid 01234567-89ab-4cde-8f01-23456789abcd
```

The first command emits the anchor-only and full-allowlist discovery stages.
The second emits a hash-bound authorization plan and confirmation token.
Neither emits a write command.

The loader-neutral PocketBlue sdm670 experiment, its verified dracut/LVM
contract, exact artifact hashes, safe interrupted-import checkpoint, and
resumable continuation are recorded in the
[PocketBlue sdm670 LVM runbook](docs/pocketblue-sdm670-lvm.md).

## Frankensargo hardware bring-up

Two sargos are in play. `dev-sargo` is the future daily driver and remains
outside this project's target set. On 2026-07-11 it was used only as an
intermediate SSH/USB host: no block-device or storage-layout operation targeted
it, although temporary lab tools and traces were staged under `/var/tmp`.

`frankensargo`, fastboot serial `99NAY1AZG1`, is the experimental target for
this project.

That downstream attempt exposed a sustained USB bulk-OUT failure in
`dev-sargo`'s experimental host path. PocketBoot never started. The topology,
isolation matrix, xHCI/usbmon evidence, cleanup state, and next controls are in
the [dev-sargo USB host-mode failure handover](docs/dev-sargo-usb-host-failure-handover.md).

Discovery on frankensargo starts read-only and with its explicit serial. The
first direct-desktop transient boot passed on 2026-07-11:

```sh
bin/probe-fastboot --serial 99NAY1AZG1
(cd out/pocketboot && sha256sum -c pocketboot-sargo-lab.img.sha256)
fastboot -s 99NAY1AZG1 boot "$PWD/out/pocketboot/pocketboot-sargo-lab.img"
sudo modprobe cdc_acm
tio /dev/ttyACM0

adb -s 99NAY1AZG1 get-state
adb -s 99NAY1AZG1 shell id
adb -s 99NAY1AZG1 shell uname -a
adb -s 99NAY1AZG1 shell cat /sys/block/mmcblk0/device/cid
adb -s 99NAY1AZG1 shell lsblk
adb -s 99NAY1AZG1 shell fdisk -l /dev/mmcblk0

mkdir -p out/inventory
bin/inventory-pocketboot --serial 99NAY1AZG1 \
  >out/inventory/frankensargo.json
jsonschema schema/frankensargo-inventory-v1.schema.json \
  -i out/inventory/frankensargo.json
```

The JSON Schema check is structural. Evidence consumers must also recompute
the canonical hash and apply the semantic/device-binding checks described in
the [inventory snapshot contract](docs/inventory-snapshot-v1.md); schema-valid
JSON alone is not verified evidence or takeover authorization.

The host-side [PBREAD1 backup controller](docs/pbread1-backup.md) turns that
inventory into an immutable, exact-serial run manifest and a resumable 64 MiB
chunk plan. It validates a device-produced identity/hash envelope for every
chunk, atomically journals only verified data, assembles a raw image, and
requires an independent full source hash to match the freshly read destination
hash. `--dry-run` and `--offline-source` exercise the complete host workflow
without contacting a phone; no userdata write is part of this tool.

The verified 7,831,552-byte image had SHA-256
`98983cc3331de0f08d6a578b89f87f2b5003607e30cb7ae5d218eb56612d48a6`.
PocketBoot displayed its UI and accepted touch input. It re-enumerated as USB
`1d6b:0104`, product `pocketboot`, with the same serial and CDC ACM, fastboot,
ADB, and mass-storage functions. The desktop needed `cdc_acm` loaded before
`ttyACM0` appeared.

The ACM log showed all eight CPUs, the 59,640 MiB eMMC, and its GPT, including
3,116 MiB each for `system_a` and `system_b`, 768 MiB each for `vendor_a` and
`vendor_b`, and 51,163 MiB for `userdata`. The generic artifact intentionally
had no VG binding, rejected userdata as a legacy nested-MBR source because it
has no MBR signature, discovered no boot entries, and held in PocketBoot for
UI or fastboot. Its eight UMS LUNs reported no media. No flash, erase, reboot,
slot change, or block-device mutation was issued.

The exact-serial ADB endpoint reported recovery mode and an unauthenticated
root shell on Linux `7.1.2+`. The eMMC CID is
`13014e53304a394b381011182ce76600`; the kernel's `lsblk` and
`/proc/partitions` views independently confirmed one 61,071,360 KiB eMMC and
72 GPT partitions. The collector independently parses the raw primary and
backup GPT structures, verifies both header CRCs and the entry-array CRC,
rejects overlaps, and then compares every used entry with the kernel's uevent,
start, and size views. Its live canonical snapshot hash was
`sha256:45fd308cf74558665e1b33ff4e5d488c88634afcb5240fc7a99e7df24fbd3ade`.
The [inventory snapshot contract](docs/inventory-snapshot-v1.md) defines the
hash and fixed read-only command set.

The large candidate observations are:

| Name | Size | Type GUID | PARTUUID |
|---|---:|---|---|
| `system_a` | 3,116 MiB | `97d7b011-54da-4835-b3c4-917ad6e73d74` | `e47e2a5d-0c65-4c57-a1c3-e4b6fdd5f56f` |
| `system_b` | 3,116 MiB | `77036cd4-03d5-42bb-8ed1-37e5a88baa34` | `803e64f5-4978-444b-9704-cb5fc2ed762c` |
| `vendor_a` | 768 MiB | `97d7b011-54da-4835-b3c4-917ad6e73d74` | `efb2cd24-9ee1-4193-b3a1-8c0cedd68f12` |
| `vendor_b` | 768 MiB | `77036cd4-03d5-42bb-8ed1-37e5a88baa34` | `2672dbbc-7d6c-46ee-9ad0-b9643c5a40cf` |
| `userdata` | 51,163 MiB | `1b81e7e6-f50d-419b-a739-2aeef8da3335` | `db04e713-11c3-4d68-bec2-8cc483bd3891` |

`fdisk` reported a valid GPT whose disk GUID is the all-zero UUID. Direct
16-byte reads from both the primary and backup GPT headers confirmed that this
is on disk, not a display bug. The disk GUID therefore contributes no device
uniqueness on frankensargo: every inventory and authorization must bind it to
the exact serial and eMMC CID. These observed kernel names and PARTUUIDs are
evidence, not takeover authorization.

The backup header is also CRC-valid but points its entry-array LBA to `2`, the
primary table. Frankensargo therefore has two headers but only one partition
entry array; the collector records `backup_entry_array_layout` as
`aliases-primary`. A raw off-device GPT capture and verified restoration test
are prerequisites to any storage write.

The same collector completed once through a Steam Deck/tailnet USB/IP path.
ADB, fastboot, ACM, and the FTDI UART all worked, but a later combined
phone-plus-UART import wedged desktop VHCI/SCSI after the Deck disappeared.
The imports eventually unwound without a reboot. The exact topology, security
changes, evidence, cleanup state, and phone-only retry controls are in the
[Steam Deck USB/IP handover](docs/steamdeck-usbip-handover.md).

The preferred Deck path was then proven on 2026-07-12 without USB/IP. A
replacement data cable exposed exact-serial stock fastboot directly to the
Deck; `fedora-latest` issued the transient PocketBoot boot and subsequently
used its packaged `fastboot` and `adb` unprivileged against the gadget. Its
physical `ttyUSB1` UART produced an unauthenticated root `/bin/sh` on
`ttyMSM0`: `google,sargo`, serial `99NAY1AZG1`, and eMMC CID
`13014e53304a394b381011182ce76600` all matched the USB/ADB inventory. The
handover records the reproducible distrobox commands, stable topology path,
and the deliberate physical-root-shell security boundary.

An earlier bounded-read control ended in a target-side ACM/configfs teardown
deadlock after a USB `EPROTO`. The latest safe-teardown control reproduced the
same transfer failure but dismantled the gadget cleanly and retained a
respawning UART getty; `userdata` was still read-only and no mutating LVM
command had run. Frankensargo currently needs a physical reset into ABL.
Two fresh-boot controls are staged on the Deck: a no-ACM image, SHA-256
`3e5fa16a…`, and an ACM plus DWC3 `tx-fifo-resize` plus safe-teardown image,
SHA-256 `4b628a5c…`.
The latter includes a respawning UART getty, read-only UMS and full SysRq. The
exact paths, hashes, patch trees, observed transfer boundary, teardown stack
and one-boot comparison rule are in the
[Steam Deck handover](docs/steamdeck-usbip-handover.md). Neither image is yet
evidence that large USB reads work.

The remaining sequence is deliberately incremental:

1. use one fresh ABL boot per USB control and prove a repeatable large PBREAD;
2. recapture the canonical inventory and make a complete, independently
   rehashed 53,648,801,280-byte off-device `userdata` backup;
3. run the hardened takeover executor through its read-only step-0 preflight;
4. export and verify the exact hash-bound bootstrap plan and explicit consent;
5. authorize only `userdata` as the anchor PV and checkpoint every mutation;
6. create `ggmeta`, `boot-rescue`, critical LVs, and the thin-data pool;
7. import and locally hash-verify Duranium before publishing its disk LV;
8. build the observed-UUID-bound PocketBoot capsule and boot Duranium; and
9. import factory artifacts without touching donors, then authorize each later
   donor independently, with a reboot while it is fenced
   before releasing its extents for allocation.

No guessed `/dev/mmcblk0pNN` name is ever authority. No `pvcreate`, `vgextend`,
slot switch, reboot, flash, or serial write belongs in discovery.

## Distro shapes

- **PocketFed** is the first direct-filesystem root. It needs LVM in its dracut
  image, a UKI/BLS publisher instead of `aboot-deploy`, and homed PAM enablement.
- **Mobian**, **Duranium**, and eventually **BengalOS** fit naturally as thin
  whole-disk LVs. PocketBoot scans their nested GPT boot partitions; their own
  repart/sysupdate/verity layout can remain intact.
- **Duranium/postmarketOS** currently builds systemd without homed/userdb, so
  shared encrypted homes require a package rebuild and first-boot adaptation.

The shared `home` filesystem is not the encryption boundary. It stores
per-user LUKS2 `name.home` images managed by systemd-homed. Every participating
OS also mounts the same thick `homed-state` LV at `/var/lib/systemd/home` so the
identity records and signing keys agree. All installed roots are therefore in
one trust domain.

## Recovery facts

- A VG spanning partitions on one eMMC adds capacity, not redundancy.
- Thin metadata, its spare, `ggmeta`, rescue bootfs, home, and homed state stay
  thick and pinned to the userdata anchor.
- A read-only exposed thin LV still requires its thin-pool dependency; pool
  activation is not claimed to be physically metadata-write-free. Pool health
  and transaction IDs are observed around every normal activation.
- PocketBoot provides 115200-baud UART, SysRq, root ADB, pstore/ramoops, and
  userspace fastboot. These lab interfaces are intentionally unauthenticated.
- Its default SysRq mask permits immediate reboot but not the complete
  sync/remount sequence; enable the full mask only in the controlled lab image.
- PocketBoot's userspace-fastboot staging limit is 256 MiB. Large artifacts are
  streamed/imported from a booted takeover environment, not flashed through it.
- Running Android dynamic-partition or factory-image tools against a donor
  after absorption will destroy the VG. Restoring Android is a planned storage
  transaction, not an incidental factory flash.
