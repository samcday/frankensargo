# PocketBoot grey-goo takeover protocol v1

This document defines the first host-verifiable protocol for gradually adding
partitions on frankensargo to a PocketBoot-owned LVM volume group. It separates
read-only inventory and archival work from the one transition that destroys a
donor partition.

The protocol is deliberately not an unattended installer. A tool may prepare
artifacts inside storage that is already owned, but it must not cross a new
partition boundary without a hash-bound authorization for that exact
partition.

The authoritative machine-readable files are:

- [`grey-goo-manifest-v1.schema.json`](../schema/grey-goo-manifest-v1.schema.json),
  the pragmatic structural schema;
- [`grey-goo-manifest-v1.example.json`](../examples/grey-goo-manifest-v1.example.json),
  a valid but entirely synthetic manifest;
- [`plan-discovery`](../target/plan-discovery), the host-only two-stage
  outer-LVM discovery validator; and
- [`plan-absorb`](../target/plan-absorb), the host-only semantic validator and
  authorization-plan generator.

## Scope and trust boundary

The Android boot slots remain outside LVM because ABL consumes them. A
PocketBoot capsule in a boot slot is also the recovery environment when the VG
or thin pool cannot be activated. Linux boot filesystems, UKIs, raw distro
disks, Android artifacts, metadata, and shared data may live inside LVM.

The two planners read only a manifest file. They do not inspect live hardware,
open a block device, mutate the manifest, or emit a storage-writing command.
Their output proves that one manifest snapshot is internally consistent; it
does not prove that the phone still matches that snapshot. A future target
executor must repeat the device, geometry, holder, source-hash, PV, VG, and
thin-pool checks immediately before any write.

The synthetic example contains no data captured from a real phone and must
never be treated as an authorization source.

## Non-negotiable invariants

1. A phone is identified by `product=sargo`, a device UUID, fastboot serial,
   eMMC CID, GPT disk GUID, and inventory hash.
2. A physical partition is identified by its parent GPT disk GUID, GPT unique
   partition GUID, partition label, start LBA, sector count, and logical sector
   size. Kernel partition numbers are observations, not identity.
3. GPT identity and geometry remain unchanged during absorption. The newly
   planned LVM PV UUID is the on-media ownership marker.
4. A planned PV UUID is random, unique, and recorded before the first
   destructive write.
5. Exactly one donor and one operation may be active. A blanket authorization
   to absorb several partitions is invalid.
6. Importing may write only to already-owned storage. The donor remains
   read-only until a durable `absorb-intent` exists.
7. A byte-for-byte, independently verified off-device copy is mandatory. A
   same-eMMC thin LV is a convenience copy, not a backup.
8. Every physical extent referenced by Android logical-partition metadata must
   be accounted for before its containing donor becomes `ready`.
9. A donor marked `firmware_gate_required` cannot become authorizable until
   the firmware bundle has been hashed, copied off-device, and load-tested.
10. Metadata, boot filesystems, thin metadata and its spare, homed state, and
    other irreplaceable state use thick LVs pinned to the userdata anchor.
11. Thin data may expand onto reclaimed donors, but thin metadata and its spare
    must not.
12. Allocation always names allowed PVs explicitly and is verified afterward.
    Default LVM placement is not an authority.
13. Initial outer-LVM discovery is restricted to the capsule-bound userdata
    anchor PARTUUID and VG UUID. After the thick metadata LV is read, the full
    scan is restricted to the manifest's physical PARTUUID/PV UUID pairs. It
    must not recursively discover LVM signatures inside guest disk LVs.
14. After the first destructive write, recovery proceeds forward or through a
    separately authorized restore transaction. The lifecycle state is never
    casually decremented.

Bootloader-consumed, radio, modem, persist, metadata, misc, FRP, GPT, and
similar control partitions are forbidden donors. `userdata` is the explicit
bootstrap anchor. Retrofit-dynamic `system_a` and `system_b` are candidates
only after real-device inventory establishes their dependencies.

## Identity and content addressing

Partition IDs have this exact form:

```text
gpt:<lowercase-disk-guid>/<lowercase-partition-guid>
```

The ID is redundant with the identity object on purpose. A planner must reject
the record unless both representations agree and the parent disk GUID equals
the device inventory's GPT disk GUID.

Frankensargo's observed GPT disk GUID is the all-zero UUID in both header
copies. The format above deliberately preserves that on-media fact; it does
not make the partition ID globally unique. The live device scope must already
match the explicit serial and eMMC CID, and the full partition tuple and
inventory hash must still match, before a zero-parent-GUID ID is accepted.

Artifact object IDs are their expanded raw content hashes:

```text
sha256:<64 lowercase hexadecimal digits>
```

For sparse factory files, the sparse-file hash may be useful provenance, but
the object ID used by this protocol is the hash of the logical byte stream
that a block device exposes. An unwritten block in a newly created thin LV
reads as zero; a sparse importer may exploit that fact only if a complete
destination read produces the recorded raw hash.

LVM tags are discovery hints rather than identifiers. Recommended v1 tags are:

```text
pocketboot.vg.v1
greygoo.anchor
greygoo.member
pocketboot.meta.v1
pocketboot.bootfs.v1
pocketboot.disk.v1
pocketboot.android.logical.v1
greygoo.archive.v1
greygoo.critical
greygoo.replaceable
```

VG, PV, and LV UUIDs remain authoritative if a human-readable name or tag is
changed.

## Immutable authorization binding

When a donor reaches `ready`, its identity, geometry, raw content record, and
planned PV UUID are frozen. The manifest stores a binding hash over this JSON
object:

```json
{
  "device_uuid": "...",
  "partition_id": "...",
  "parent_disk_guid": "...",
  "partuuid": "...",
  "partlabel": "...",
  "start_lba": "...",
  "sectors": "...",
  "logical_sector_bytes": 512,
  "raw_bytes": "...",
  "raw_sha256": "sha256:...",
  "planned_pv_uuid": "..."
}
```

Canonical JSON in protocol v1 means the compact, recursively key-sorted output
of `jq -cS`, followed by exactly one LF byte. It is not claimed to be RFC 8785.
The binding value is `sha256:` followed by the SHA-256 of those bytes.

Decimal storage quantities are strings. This avoids accidental loss of
precision in JSON consumers. The v1 host planner deliberately accepts at most
18 decimal digits so all arithmetic remains exact in a signed 64-bit shell.

## Lifecycle

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

`quarantined` is reachable from any state when an identity, content, LVM, or
thin-metadata observation is inconsistent.

- `discovered`: read-only GPT and Android metadata inventory exists.
- `copying`: a resumable copy is incomplete; the source is still intact.
- `archive-verified`: complete source and destination reads have the same hash.
- `ready`: the off-device copy, dependency graph, applicable firmware gate,
  and net-capacity calculation are all satisfied.
- `authorized`: an operator has confirmed an authorization bound to the
  current manifest and donor.
- `absorb-intent`: the operation record and recovery metadata are durable. An
  already authorized transaction may now resume after a reset.
- `pv-present`: the planned PV label is observable, although the PV may still
  be orphaned.
- `vg-member-fenced`: the PV is in the expected VG with allocation disabled.
- `capacity-released`: a recovery boot has succeeded and allocation is
  enabled.
- `absorbed`: the planned thin-data or thick-LV allocation is observable on the
  donor.

The controller writes an intent record before each irreversible step and then
records completion. On restart it reconciles physical facts forward rather
than blindly replaying the last action.

## Bootstrap and expansion

The first explicitly destructive operation turns `userdata` into the anchor
PV from a host-retained bootstrap plan. Until the VG metadata LV exists, that
host plan and the planned anchor PV UUID are the recovery record.

The bootstrap creates:

- thick metadata and boot-filesystem LVs;
- final-capacity-sized thin metadata and an equally usable metadata spare,
  both on the anchor;
- a deliberately small initial thin-data LV;
- sufficient unallocated anchor space for recovery and artifact import.

Normal PocketBoot retains only the stable anchor PARTUUID and VG UUID binding.
It first offers that one physical device to LVM and activates only the
userdata-pinned metadata LV read-only in partial mode. After validating the
manifest, it resolves the complete allowed PV set and performs full read-only
boot-LV activation. This avoids both a circular manifest dependency and a
capsule rewrite whenever the goo grows.

The v1 manifest represents this as a top-level `capsule_binding` plus
`lvm.allowed_pvs`, whose entries bind a GPT-derived partition ID and PARTUUID
to an observed PV UUID. `target/plan-discovery` rejects duplicate tuple
components, a tuple from another GPT disk, a capsule/VG/anchor mismatch, a
candidate already admitted as a PV, or a later PV without a matching member
record in `vg-member-fenced`, `capacity-released`, or `absorbed` state. Its
stage-one output contains exactly the anchor and metadata tag; stage two
contains the complete tuple allowlist and bootable LV tags.

Before either Android system container is reclaimed, the importer records all
logical-partition metadata slots, groups, attributes, and physical extents. It
preserves the raw metadata bytes, hashes the entire container, and confirms
that mappings made from the archive describe the same bytes as mappings made
from the source.

When a donor first joins the VG it remains non-allocatable. A recovery boot
must prove that PocketBoot activates only the expected outer VG and can still
read the archive and metadata. Thin-data expansion then names that donor
explicitly and verifies that no critical LV or thin-metadata extent moved.

Existing thin archive blocks must not later be moved onto the partition from
which they were captured. Tooling therefore forbids casual movement of
thin-data segments.

## Conservative capacity gate

The planner recomputes, rather than trusts, this equation:

```text
conservative_net_bytes =
    donor_bytes
  - retained_allocated_bytes
  - metadata_growth_bytes
  - reserve_bytes
```

`donor_bytes` must equal both the raw object byte count and
`sectors * logical_sector_bytes`. Every deduction is non-negative, and the
result must be exactly equal to the manifest value and greater than zero.

This makes an awkward truth visible: retaining a thick byte-for-byte copy on
the same eMMC yields almost no reclaimed capacity. The intended useful
combination is an off-device raw backup plus verified sparse local raw or
logical artifacts.

## Archive and firmware gates

The partition's `archive_object_id` must select exactly one immutable object.
That object must bind back to the exact partition ID and agree on byte count,
source hash, and destination hash. At least one copy must have:

- `location_class` equal to `off-device`;
- the same expanded byte count and SHA-256;
- `verified` equal to `true`.

When firmware is required, all of these are mandatory:

- global firmware status `satisfied`;
- a well-formed bundle SHA-256;
- a verified off-device bundle;
- a successful recorded load test for the intended kernel build.

Merely downloading a factory image or extracting filenames is not sufficient.

For an Android LP container the selected partition also carries an LP extent
gate. It binds hashes of the raw LP metadata and reverse extent graph, asserts
that every touching extent has been archived, and records that logical maps
reconstructed independently from source and archive expose identical bytes.
The raw archive object must be typed `android-lp-container`. A `ready` label
without this satisfied gate is not authorizable.

## Authorization planning

Generate a plan with an exact partition ID and a new operation UUID:

```sh
target/plan-absorb \
  --manifest examples/grey-goo-manifest-v1.example.json \
  --partition-id gpt:11111111-2222-4333-8444-555555555555/66666666-7777-4888-9999-aaaaaaaaaaaa \
  --operation-uuid 01234567-89ab-4cde-8f01-23456789abcd
```

The example invocation is safe because it only reads synthetic JSON.

The planner requires:

- exactly one matching `candidate` partition in state `ready`;
- immutable identity, geometry, raw hash record, and planned PV UUID;
- a recomputed binding hash match;
- geometry and raw byte-count agreement;
- exactly one matching immutable archive object;
- a matching verified off-device copy;
- a satisfied Android LP metadata/extent/mapping gate when required;
- a satisfied firmware gate when required;
- a valid capsule binding and current physical PARTUUID/PV UUID allowlist;
- `active_operation` equal to JSON `null`;
- an exact positive conservative capacity result.

The manifest hash is SHA-256 over its protocol-v1 canonical JSON plus one LF.
The authorization core includes that manifest hash and every field needed to
identify the device, donor, archive, firmware result, capacity calculation,
VG, and planned PV UUID. Its `authorization_sha256` is calculated using the
same canonicalization rule before the hash and confirmation fields are added.

The confirmation token is deterministic:

```text
ABSORB-<first operation UUID group>-<first 12 authorization hash digits>
```

Typing that token into a future executor authorizes only the bound operation.
The plan itself performs no action, and a token from a different generation,
partition, device, or operation is not interchangeable.

## Reset and recovery decisions

On every boot, observed media state wins over a stale lifecycle label:

| Observation | Recovery decision |
| --- | --- |
| Incomplete staging LV; donor hash intact | Resume or recreate staging |
| Destination exists but is not verified | Read and hash the entire destination |
| `absorb-intent`; donor still intact | Resume only that authorization or explicitly cancel |
| Donor partly cleared; no valid PV | Require the exact durable intent and archive, then continue forward |
| Planned PV exists but is orphaned | Add that exact PV to the expected VG; do not relabel it |
| Planned PV is already in the VG | Adopt the observed state and fence allocation |
| VG metadata copies disagree | Quarantine; reconcile a consistent VG sequence before writes |
| Thin data is larger than the manifest | Check thin metadata, then adopt observed LVM state |
| UUID, geometry, or signature is unexpected | Quarantine for manual recovery |

Cancellation is cheap through `ready`. After the first destructive write,
restoration requires evacuating all extents, proving the PV unused, removing
it from the VG, restoring the raw archive, verifying its complete hash, and
recreating the original Android mappings. That is a distinct destructive
transaction and may require external staging as large as the donor.

## What JSON Schema does not prove

The v1 schema intentionally covers shape, required fields, basic formats, and
enumerations. It cannot express all of the following conveniently:

- uniqueness of partition or object IDs by a selected property;
- cross-references between partitions, objects, and off-device copies;
- binding-hash or manifest-hash recomputation;
- exact geometry and capacity arithmetic;
- conditional firmware policy for a selected donor;
- capsule/anchor/allowed-PV agreement and lifecycle membership;
- the Android LP metadata/extent gate for a selected donor;
- agreement with live hardware.

`plan-discovery` implements the outer-LVM mapping and membership checks.
`plan-absorb` runs it first, then implements the selected-donor cross-reference,
hash, arithmetic, LP, and firmware checks. A future executor is responsible for
the final live-hardware agreement and must fail closed.
