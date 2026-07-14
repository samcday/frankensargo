# Duranium post-bootstrap deployment runbook

This runbook starts only after the authorized userdata bootstrap has completed
and every step through `create-duranium-thin-disk` has a durable, verified
checkpoint. It does not authorize the bootstrap itself. The target contract is:

| object | required state before import | published state |
|---|---|---|
| VG | exact observed UUID, name `franken`, tag `pocketboot.vg.v1` | unchanged |
| `boot-rescue` | 2 GiB thick LV on the userdata PV; tags `greygoo.critical`, `pocketboot.bootfs.v1` | ext4, normally activated read-only by PocketBoot |
| `pool` | 20 GiB thin-data LV, 256 KiB chunks, `errorwhenfull=y`, `discards=nopassdown` | at least 16 GiB data headroom after this import |
| `disk-duranium` | 20 GiB thin LV; tags `distro.duranium`, `greygoo.import-pending`, `greygoo.replaceable`; permission `rw` | permission `r`; pending tag removed; `pocketboot.disk.v1` added |

The human-readable names are not identity. Substitute only PV, VG, and LV
UUIDs copied from the fsynced bootstrap checkpoints and then matched against a
fresh fenced LVM report. Do not copy UUIDs from examples or this document.

## Known inputs and current gaps

The audited Deck inputs are under
`/home/deck/frankensargo-lab/artifacts/duranium`:

- `google-sargo_phosh_edge_26070701.raw.zst` — SHA-256
  `035fa4b4f1ea70f6d2706f7e0d60e4c3f97d36f7571de739fbb29eab02f99f68`;
- decompressed raw disk — 6,862,950,400 bytes, SHA-256
  `1e911b82a87325a6c3a5624cbbcd8c157d2c4a3abca6dd568b6c49727eb00e34`;
- `google-sargo_phosh_edge_26070701.efi` — 103,735,296 bytes, SHA-256
  `eedefda43cb97ced8d4be0b6a50c1354cbb840ac798c3d795dabb6972e213757`.

The unmodified raw has 6,014 nonzero 256 KiB chunks, so a zero-skipping
import maps at most 1,576,534,016 bytes before the small ESP adapter change.
The derived image must be measured again; the raw count is evidence, not the
authorization value.

At the time this runbook was written, the Deck had no built adapter and no
derived disk. `frankensargo-lab/pocketboot-current` also named the historical
`7860da…` image, not the newer `53cdf4…` image. Never infer "current" from that
directory name: select a PocketBoot image by its verified hash and provenance.

Current build and transport rules are:

1. `bin/build-duranium-lvm-adapter` now emits one canonical four-element
   `required_cmdline` list. `bin/audit-duranium-import` independently enforces
   that exact list before accepting a derived disk.
2. The generic `bin/build-pocketboot` artifact is intentionally unbound. Use
   `bin/build-pocketboot-bound` after the VG exists; never edit the device TOML
   and invoke `cargo xtask` directly. The wrapper journals and restores its one
   temporary source edit, hashes both trees, and parses the completed Android
   header before emitting bound provenance.
3. The current PocketBoot patch stack deliberately hardwires UMS LUNs
   read-only. Keep that property for verification. Formatting and import must
   instead use the import controller's bounded raw ADB input path or a future
   separately reviewed takeover-only writer. Never weaken normal UMS globally
   merely to make an import convenient.
4. Neither multi-gigabyte raw ADB input nor read-only UMS output is presently
   hardware-qualified. Every large device-to-host path tested in this bring-up
   has failed at roughly the first 16 KiB boundary. Patch
   `0010-enable-sha256sum.patch` therefore adds BusyBox `sha256sum` and bumps
   its build recipe: the publication gate is a full local hash of the exact
   read-only LV, compared with the independently computed padded-image hash.
   Only the small, identity-bound attestation crosses ADB. PocketBoot patch
   `0013-adb-shell-v2-status.patch` and `lib/adb_shell_v2.py` give both the
   bootstrap and import controllers one common raw, non-PTY `shell_v2`
   transport. It proves the exact serial, feature negotiation, a fresh nonce,
   separate stdout/stderr and a typed exit frame; every real success is mapped
   to reserved remote status 173 so legacy ADB's unconditional host status zero
   is rejected on every command. File-backed stdin supplies bounded `dd`
   extents without buffering them. The controller refuses all activation and
   writes when this proof is unavailable.
   UMS remains useful
   as optional secondary evidence once qualified; it is not the primary gate.

There is not yet a real rescue UKI bundle. The first pass therefore formats
and records evidence on `boot-rescue`, but deliberately publishes no BLS or
Type #2 entry there. PocketBoot orders XBOOTLDR entries ahead of nested ESP
entries; an accidental rescue entry would otherwise outrank the Duranium
nested-GPT proof.

## 1. Freeze the observed LVM identities

The bootstrap executor records all generated IDs in its final
`steps/12-backup-vg-metadata.json` checkpoint. On the durable Deck state
directory, derive at least:

```sh
export SERIAL=99NAY1AZG1
export USERDATA_PARTUUID=db04e713-11c3-4d68-bec2-8cc483bd3891
export FINAL_CHECKPOINT="$DURABLE_STATE/steps/12-backup-vg-metadata.json"
PV_UUID=$(jq -er '.generated_ids.pv_uuid' "$FINAL_CHECKPOINT"); export PV_UUID
VG_UUID=$(jq -er '.generated_ids.vg_uuid' "$FINAL_CHECKPOINT"); export VG_UUID
BOOT_LV_UUID=$(jq -er '.generated_ids.lv_uuids["boot-rescue"]' "$FINAL_CHECKPOINT"); export BOOT_LV_UUID
DISK_LV_UUID=$(jq -er '.generated_ids.lv_uuids["disk-duranium"]' "$FINAL_CHECKPOINT"); export DISK_LV_UUID
```

In PocketBoot, re-resolve `userdata` by an exact sysfs PARTUUID scan. The
expected node observation is `/dev/mmcblk0p72`, but that name is not authority.
Require exactly one non-removable partition match, no mounts, swaps, holders,
or active UMS LUN, and then use only the resolved node as `$ANCHOR`.

Run all reports with the same scan fence:

```sh
/sbin/lvm.static pvs --devices "$ANCHOR" --nohints --readonly \
  --config 'backup/backup=0 backup/archive=0' \
  --noheadings --unquoted --separator '|' \
  -o pv_uuid,pv_name,pv_size,pv_free,pv_tags,vg_uuid,vg_name "$ANCHOR"

/sbin/lvm.static lvs --devices "$ANCHOR" --nohints --readonly \
  --config 'backup/backup=0 backup/archive=0' \
  --noheadings --unquoted --separator '|' -a \
  -o vg_uuid,lv_uuid,lv_name,lv_size,lv_permissions,lv_tags,lv_attr,segtype,pool_lv,data_percent,metadata_percent,devices \
  franken
```

Compare the complete output to the bootstrap checkpoints. In particular,
`disk-duranium` must still be a writable, inactive thin LV with the pending
tag and without `pocketboot.disk.v1`. Persist and fsync this pre-import report.

## Local full-LV attestation contract

The PocketBoot image used for either write transaction must have a checksum
and provenance file that includes `0010-enable-sha256sum.patch`; archive those
files with the transaction. At runtime, `/usr/bin/sha256sum` must resolve to
the image's installed `/bin/busybox`. This does not authorize an LV by name.
Before hashing, a fresh fenced `lvs` report must match its checkpointed VG/LV
UUID, role tags, pool, permission, and size. Set `$FRANKENSARGO` to the exact
checkout used for the build, not merely another checkout at a similar branch.

```sh
EXPECTED_IMAGE_SHA256=$(awk 'NF == 2 { print $1 }' \
  "$POCKETBOOT_IMAGE.sha256")
ACTUAL_IMAGE_SHA256=$(sha256sum "$POCKETBOOT_IMAGE")
ACTUAL_IMAGE_SHA256=${ACTUAL_IMAGE_SHA256%% *}
test "$ACTUAL_IMAGE_SHA256" = "$EXPECTED_IMAGE_SHA256"
SHA_PATCH=0010-enable-sha256sum.patch
SHA_PATCH_DIGEST=$(sha256sum "$FRANKENSARGO/patches/pocketboot/$SHA_PATCH")
SHA_PATCH_DIGEST=${SHA_PATCH_DIGEST%% *}
grep -Fx "patch=$SHA_PATCH sha256=$SHA_PATCH_DIGEST" \
  "$POCKETBOOT_IMAGE.provenance"
```

Generate a separate verifier for each LV. Set `ROLE`, `LV_UUID`, `LV_PATH`,
`EXPECTED_SECTORS`, and `EXPECTED_SHA256` to the exact values called out below:

```sh
VG_COMPACT=$(printf '%s' "$VG_UUID" | tr -d '-')
LV_COMPACT=$(printf '%s' "$LV_UUID" | tr -d '-')
EXPECTED_LVM_DM_UUID="LVM-${VG_COMPACT}${LV_COMPACT}"
VERIFY_SCRIPT="$DURABLE_STATE/verify-${ROLE}-lv.sh"

cat >"$VERIFY_SCRIPT" <<EOF
#!/bin/sh
set -eu
ROLE='$ROLE'
LV_PATH='$LV_PATH'
EXPECTED_LVM_DM_UUID='$EXPECTED_LVM_DM_UUID'
EXPECTED_SECTORS='$EXPECTED_SECTORS'
EXPECTED_SHA256='$EXPECTED_SHA256'

SHA_APPLET=\$(/usr/bin/readlink -f /usr/bin/sha256sum)
test "\$SHA_APPLET" = /bin/busybox
SELF_SHA256=\${1:?missing-verifier-sha256}
SELF_ACTUAL=\$(/usr/bin/sha256sum "\$0")
SELF_ACTUAL=\${SELF_ACTUAL%% *}
test "\$SELF_ACTUAL" = "\$SELF_SHA256"
test -L "\$LV_PATH"

check_identity()
{
    DM_NODE=\$(/usr/bin/readlink -f "\$LV_PATH")
    case \$DM_NODE in /dev/dm-[0-9]*) ;; *) exit 41 ;; esac
    DM_NAME=\${DM_NODE##*/}
    SYSFS="/sys/class/block/\$DM_NAME"
    ACTUAL_LVM_DM_UUID=\$(/bin/cat "\$SYSFS/dm/uuid")
    ACTUAL_SECTORS=\$(/bin/cat "\$SYSFS/size")
    ACTUAL_RO=\$(/bin/cat "\$SYSFS/ro")
    test "\$ACTUAL_LVM_DM_UUID" = "\$EXPECTED_LVM_DM_UUID"
    test "\$ACTUAL_SECTORS" = "\$EXPECTED_SECTORS"
    test "\$ACTUAL_RO" = 1
}

check_identity
ACTUAL_SHA256=\$(/usr/bin/sha256sum "\$DM_NODE")
ACTUAL_SHA256=\${ACTUAL_SHA256%% *}
test "\$ACTUAL_SHA256" = "\$EXPECTED_SHA256"
check_identity
printf '%s\n' \
  "FRANKENSARGO_LV_SHA256_V1|role=\$ROLE|script_sha256=\$SELF_SHA256|sha_applet=\$SHA_APPLET|lvm_dm_uuid=\$ACTUAL_LVM_DM_UUID|dm_node=\$DM_NODE|sectors=\$ACTUAL_SECTORS|ro=\$ACTUAL_RO|expected_sha256=\$EXPECTED_SHA256|actual_sha256=\$ACTUAL_SHA256"
EOF

VERIFY_SCRIPT_SHA256=$(sha256sum "$VERIFY_SCRIPT")
VERIFY_SCRIPT_SHA256=${VERIFY_SCRIPT_SHA256%% *}
```

The shell above specifies the verifier payload contract; it is not an
authorization to push or execute it with hand-written `adb exec-*`. The import
controller performs the equivalent identity-bound full-LV hash through the
shared raw `shell_v2` primitive and journals its exact argv, typed remote
status, PocketBoot image provenance, stdout/stderr hashes and pre/post LVM pool
reports. Empty, partial, duplicate, extra, legacy, disconnected or status-less
output fails closed. The identity is the full `LVM-<VG><LV>` UUID plus size and
read-only state; `/dev/mapper/franken-*` is only its lookup path.

UART may carry the same verifier command and one-line result when ADB control
is unavailable. A timestamped UART transcript is useful fallback evidence,
but it does not replace capture of the exact verifier bytes/hash, command,
PocketBoot provenance, identity checks, and expected-versus-actual digest.

## 2. Format and populate `boot-rescue`

PocketBoot's normal LVM configuration makes every `pocketboot.bootfs.v1` LV
read-only. For this one explicit maintenance transaction, activate only the
checkpointed `boot-rescue` LV with the read-only list overridden to empty:

```sh
/sbin/lvm.static lvchange --devices "$ANCHOR" --nohints \
  --config 'devices/use_devicesfile=0 activation/auto_activation_volume_list=[] activation/read_only_volume_list=[] backup/backup=0 backup/archive=0' \
  --activate y franken/boot-rescue
```

Before writing it, prove that the resulting device-mapper UUID is the compact
concatenation `LVM-<VG_UUID><BOOT_LV_UUID>`, its size is exactly 2,147,483,648
bytes, it is writable for this transaction, and no filesystem signature is
present.

Build the complete filesystem as an ordinary sparse host file first. `mke2fs
-d` populates from the staging tree without a mount or root privilege:

```sh
mkdir -p "$DURABLE_STATE/boot-rescue-root/frankensargo/evidence"
cp "$PLAN" "$DURABLE_STATE/boot-rescue-root/frankensargo/evidence/bootstrap-plan.json"
cp "$DURABLE_STATE/franken.vgcfg" \
  "$DURABLE_STATE/boot-rescue-root/frankensargo/evidence/"
cp "$POCKETBOOT_IMAGE.provenance" \
  "$DURABLE_STATE/boot-rescue-root/frankensargo/evidence/"
sha256sum "$DURABLE_STATE/boot-rescue-root/frankensargo/evidence/"* \
  >"$DURABLE_STATE/boot-rescue-root/frankensargo/evidence/SHA256SUMS"

truncate -s 2147483648 "$DURABLE_STATE/boot-rescue.ext4"
mkfs.ext4 -F -b 4096 -m 0 -L FRANKEN-BOOT -U "$BOOT_FS_UUID" \
  -d "$DURABLE_STATE/boot-rescue-root" "$DURABLE_STATE/boot-rescue.ext4"
e2fsck -fn "$DURABLE_STATE/boot-rescue.ext4"
sha256sum "$DURABLE_STATE/boot-rescue.ext4" \
  >"$DURABLE_STATE/boot-rescue.ext4.sha256"
```

For the nested-GPT proof, the staging tree must contain no
`loader/entries/*.conf` and no `EFI/Linux/*.efi`. There is not yet a separately
reviewed crash-resumable writer for `boot-rescue`; do not stream this image with
legacy `adb exec-in` or a hand-written raw-shell command. Leave the LV empty
and unpublished until such a writer uses the same file-backed raw `shell_v2`
primitive, fsynced intent/result journal and full read-only hash gate as the
Duranium import controller. After that future transaction, deactivate the
writable mapping, reactivate it through the normal read-only policy, and repeat
the exact fenced LVM report. Then set:

```sh
ROLE=boot-rescue
LV_UUID=$BOOT_LV_UUID
LV_PATH=/dev/mapper/franken-boot--rescue
EXPECTED_SECTORS=4194304
EXPECTED_SHA256=$(awk 'NF == 2 { print $1 }' \
  "$DURABLE_STATE/boot-rescue.ext4.sha256")
```

Run the local full-LV attestation contract above. Its exact UUID/size/RO checks
and two matching 2 GiB hashes are the initialization gate. Record a post-hash
LVM report and require no pool or metadata mutation during this read-only
operation. A subsequently qualified read-only UMS hash and read-only ext4
mount can add filesystem-level evidence, but a guessed `/dev/sdX`, sampled
read, or current failing large-output path cannot replace the local full hash.

## 3. Build the exact adapter and derived disk

Use the observed UUIDs with the pinned tool hashes documented in
[`duranium-lvm-adapter.md`](duranium-lvm-adapter.md):

```sh
SOURCE_DATE_EPOCH=0 bin/build-duranium-lvm-adapter \
  --userdata-partuuid "$USERDATA_PARTUUID" \
  --pv-uuid "$PV_UUID" --vg-uuid "$VG_UUID" \
  --disk-lv-uuid "$DISK_LV_UUID" \
  --disk-lv-name disk-duranium --disk-lv-tag pocketboot.disk.v1 \
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
  --output "$DURABLE_STATE/frankensargo-duranium-lvm-26070701.cpio"
```

Use the pinned invocation in
[`prepare-duranium-disk.md`](prepare-duranium-disk.md) to create a new derived
raw and provenance JSON. Both output paths must be absent before the command.
The result adds the bound adapter, a Type #1 `uki` entry, and
`default frankensargo-duranium.conf` only inside the exact nested ESP.

Audit the complete contract and calculate the full 20 GiB destination hash:

```sh
bin/audit-duranium-import \
  --disk "$DERIVED_RAW" --provenance "$DERIVED_PROVENANCE" \
  --adapter "$DURABLE_STATE/frankensargo-duranium-lvm-26070701.cpio" \
  --userdata-partuuid "$USERDATA_PARTUUID" \
  --pv-uuid "$PV_UUID" --vg-uuid "$VG_UUID" \
  --disk-lv-uuid "$DISK_LV_UUID" \
  >"$DURABLE_STATE/duranium-import-audit.json"
```

This independently rehashes the disk and adapter, parses the adapter's newc
manifest, requires exact PV/VG/LV bindings and cmdline policy, counts nonzero
256 KiB chunks, and hashes the derived raw followed by zeroes to exactly
21,474,836,480 bytes. Its default gate rejects an import whose allocation
ceiling would leave less than 16 GiB of an otherwise empty 20 GiB pool.
The audit also binds `maximum_pool_metadata_percent=75.00`; the controller
treats LVM's reported precision conservatively by adding one report quantum,
so a displayed value at the limit is rejected rather than rounded down into
the safe side. `Metadata%=99` is therefore an unconditional pre-write failure.
It also emits hash-pinned, contiguous nonzero write extents split at 64 MiB,
so the host never needs to send zero-only thin chunks. Fsync the audit JSON
before activating the import target.

The gate is not a substitute for live accounting. Before **every remaining
extent**, including after a controller restart, the controller obtains a new
fenced LVM report and revalidates exact PV/VG/pool/internal-LV identities,
health, Data%, and Metadata%. It adds the current conservative data-usage upper
bound to the audited allocation ceiling for that extent and all later extents;
the result must still leave 16 GiB. Durable percentages are evidence only and
are never reused as the current capacity decision. The same current metadata
headroom gate runs before and after the full-LV hash transition and immediately
before and after publication.

## 4. Run the crash-resumable sparse-import controller

The manual `jq | while dd | adb` loop has been retired. It could not durably
distinguish a completed extent from a partial transport, and legacy ADB could
not prove the remote `dd` exit status. Use `bin/execute-duranium-import` with
the exact final bootstrap checkpoint and the independently generated audit:

```sh
bin/execute-duranium-import \
  --plan "$PLAN" \
  --bootstrap-checkpoint "$FINAL_CHECKPOINT" \
  --audit "$DURABLE_STATE/duranium-import-audit.json" \
  --disk "$DERIVED_RAW" --provenance "$DERIVED_PROVENANCE" \
  --adapter "$DURABLE_STATE/frankensargo-duranium-lvm-26070701.cpio" \
  --pocketboot-image "$POCKETBOOT_IMAGE" \
  --pocketboot-provenance "$POCKETBOOT_IMAGE.provenance.json" \
  --state-dir "$DURABLE_STATE/duranium-import-state" \
  --serial "$SERIAL" --partuuid "$USERDATA_PARTUUID" \
  --print-confirmation

# Re-run with the printed token only after reviewing every frozen path/hash.
bin/execute-duranium-import \
  --plan "$PLAN" \
  --bootstrap-checkpoint "$FINAL_CHECKPOINT" \
  --audit "$DURABLE_STATE/duranium-import-audit.json" \
  --disk "$DERIVED_RAW" --provenance "$DERIVED_PROVENANCE" \
  --adapter "$DURABLE_STATE/frankensargo-duranium-lvm-26070701.cpio" \
  --pocketboot-image "$POCKETBOOT_IMAGE" \
  --pocketboot-provenance "$POCKETBOOT_IMAGE.provenance.json" \
  --state-dir "$DURABLE_STATE/duranium-import-state" \
  --serial "$SERIAL" --partuuid "$USERDATA_PARTUUID" \
  --confirm IMPORT-DURANIUM-... --execute
```

On every invocation the controller opens and shared-locks the disk,
provenance, adapter, bound PocketBoot image/provenance, plan, checkpoint, and
audit without following a final symlink. It re-runs `audit-duranium-import`
and compares the complete canonical result rather than trusting the saved
JSON. It verifies the final bootstrap checkpoint and its durable
`vgcfgbackup`, binds the exact PV, VG, pool, internal thin-LV, and destination
UUIDs, and requires the bound PocketBoot provenance to match the complete
current patch set including read-only UMS, BusyBox SHA-256, safe teardown, and
ADB shell-v2 status.
The durable transaction additionally pins SHA-256 for the entrypoint,
controller module, shared shell-v2 client, and audit tool. The confirmation
token covers those implementation hashes. An older controller cannot reopen a
same-directory transaction while describing itself as the current policy.

Every sparse extent has an fsynced, hash-chained intent containing the exact
regenerated argv before transport. The target first stages the bounded input in
private `/run` storage, proves its exact byte count and SHA-256, writes the
identity-checked mapping, runs `sync` plus `blockdev --flushbufs`, then reads the
exact extent back and proves the same hash. Only then can shell-v2 return its
typed success and the host fsync a result. The result binds exact argv, typed
status, stdout/stderr byte counts and hashes, source evidence, durability
barrier version, and target readback hash.

An intent with no typed result is replayed from the same held bytes at the same
offset; this is the only idempotent transport-disconnect recovery. A typed
nonzero result is durably recorded and is never reclassified as a disconnect or
replayed. Before reusing any successful extent result after restart, the target
runs a fresh exact extent readback; a mismatch fails closed rather than silently
skipping a partial write. Gaps, reordering, partial/corrupt results, changed
source identity, stale transaction or implementation binding, live identity
drift, and pool-budget drift all stop publication.

After all extents, the controller flushes, rechecks the live pool, changes the
exact mapping to kernel read-only, and hashes all 20 GiB on the phone. The
attestation binds the full `LVM-<VG><LV>` device-mapper UUID, 41,943,040
sectors, read-only state, exact serial, and audit's zero-padded digest. Pool
identity, health, reserve, and conservative metadata headroom must pass fresh
live gates on both sides of this read-only hash.

That equality also proves the nested GPT bytes already audited on the host.
The imported GPT's backup header remains at the end of the published 6.86 GB
image rather than the end of the 20 GiB LV. PocketBoot deliberately parses the
bounded primary GPT, but Duranium's `losetup --partscan` behavior on this larger
backing device remains a first-hardware-boot gate; do not silently "repair"
the published GPT.

Read-only UMS or a repaired reverse-Fastboot read can later provide secondary
transport evidence, but only after independent large-transfer qualification.
It is not required to move past the known 16 KiB output failure once the exact
local full-LV attestation passes. A sampled read, an unbound `sha256sum` path,
or a digest without the verifier/image provenance still fails the gate.

Any mismatch leaves the LV unpublished and `greygoo.import-pending`; it must
not become discoverable. Re-run the same command and state directory after a
host-side crash when no typed failure was received. A durably recorded typed
nonzero is a terminal failure for that transaction state and requires diagnosis
and a newly reviewed transaction; never delete or hand-edit its journal to make
progress.

Residual limits are explicit. The durability claim is as strong as Linux's
`sync` and the eMMC/device-mapper flush implementation; the subsequent live
readback detects a missing write on every restart but cannot make broken storage
honor power-loss barriers. One extent requires up to 64 MiB of temporary `/run`
space. Finally, the capacity report and following direct write are not one
atomic LVM operation: PocketBoot's single-purpose rescue environment must have
no independent thin-pool allocator. A concurrent external allocator can race
the observation; `errorwhenfull=y`, the 16 GiB reserve, per-extent rechecks, and
the final full hash make that race fail-safe for publication, but do not turn it
into serializable multi-writer accounting.

## 5. Controller-owned publication and metadata backup

Do not run the plan's `post_import_argv` by hand. The controller accepts that
one exact reviewed argv from the bootstrap plan and executes it only after its
durable full-LV attestation record and a final inactive pending-state report.

Verify exact VG/LV UUIDs, thin attributes, persistent read-only permission,
pool UUID, absence of the pending tag, and exactly these role/lifecycle tags:

```text
distro.duranium,greygoo.replaceable,pocketboot.disk.v1
```

Only after the exact published postcondition matches does the controller run
and pull `vgcfgbackup`, validate its complete UUID set, and fsync
`franken-post-import.vgcfg` plus the terminal JSON checkpoint. A restart adopts
an already-published state only when this same transaction has a durable exact
attestation and publication intent; premature publication fails closed.

## 6. Build and prove the UUID-bound PocketBoot image

The bound image's PocketBoot kernel command line must contain exactly one of
each binding token, in addition to the existing lab/debug and SysRq policy:

```text
pocketboot.log=debug sysrq_always_enabled=1 pocketboot.vg_uuid=<OBSERVED_VG_UUID> pocketboot.pv_partuuid=db04e713-11c3-4d68-bec2-8cc483bd3891
```

Generate and inspect the bound source tree/cmdline without doing the large
build first:

```sh
bin/build-pocketboot-bound --no-acm --prepare-only \
  --serialno "$SERIAL" \
  --vg-uuid "$VG_UUID" \
  --pv-partuuid "$USERDATA_PARTUUID" \
  --kernel-tree /home/deck/src/linux-sdm670-mainline
```

Then build into a fresh destination:

```sh
bin/build-pocketboot-bound --no-acm \
  --serialno "$SERIAL" \
  --vg-uuid "$VG_UUID" \
  --pv-partuuid "$USERDATA_PARTUUID" \
  --kernel-tree /home/deck/src/linux-sdm670-mainline \
  --output-dir "$DURABLE_STATE/pocketboot-bound"
```

Here `$SERIAL` is the exact serial observed during the fenced device inventory,
not an example device identifier. The wrapper validates the serial and both
storage values, invokes the exact generic patch-stack
preparer, takes the same exclusive source lock as generic builds, journals the
original configuration, and composes removal of exactly one `pocketboot.acm`
token with the binding. It records the base/profile/bound command lines and
base/bound Git trees, builds, restores and rechecks the exact base tree, then
parses the finished Android v2 header and rejects any mismatch. Omit
`--no-acm` only for an explicit ACM control. It refuses to replace an existing
image, checksum, or provenance file. Hash and fsync all three outputs before
moving toward ABL.

Boot the resulting image transiently from ABL first. On UART, require:

- the exact bound command line and `sysrq: sysrq always enabled`;
- resolution of only the userdata PARTUUID and the exact VG UUID;
- read-only activation of `boot-rescue` and `disk-duranium` with verified
  device-mapper UUIDs;
- an empty scan of the XBOOTLDR-role `boot-rescue` filesystem;
- bounded primary-GPT parsing of `disk-duranium` and a read-only slice of its
  exact ESP;
- discovery of `frankensargo-duranium.conf` as preferred over the duplicate
  direct Type #2 UKI; and
- UI title `Duranium 26070701 (frankensargo LVM)` on the nested ESP entry.

Select that exact entry. The downstream command line must preserve the
published `usrhash=` and add `root=dissect mount.usr=dissect
sysrq_always_enabled=1`. Before systemd proper starts, UART should report:

```text
frankensargo-duranium: verified <DISK_LV_UUID> as rootdisk on /dev/loopN
```

In Duranium, verify `/run/frankensargo/duranium-rootdisk`, the loop's read-only
backing device and GPT partitions, the dm-verity `/usr` mount, the complete
kernel command line, and a nonzero `/proc/sys/kernel/sysrq`. The pinned sdm670
kernel configuration is expected to provide `CONFIG_MAGIC_SYSRQ=y` and
`CONFIG_MAGIC_SYSRQ_SERIAL=y`, but the exact published kernel has not yet been
independently config-extracted; runtime proof is still required.

If a DRM panic QR appears, stop and photograph the complete screen and QR
before any reset. Keep UART capture running. A non-destructive SysRq help test
is preferable for the first successful boot; use forced reset only for an
actual recovery condition.
