# Read-only inventory snapshot v1

`bin/inventory-pocketboot` turns one live PocketBoot ADB transport into a
deterministic hardware/GPT evidence record. It is the input to a future real
manifest builder, not a takeover authorization and not a storage writer.

## Invocation

```sh
bin/inventory-pocketboot --serial 99NAY1AZG1 >inventory.json
jsonschema schema/frankensargo-inventory-v1.schema.json -i inventory.json
```

The explicit serial is mandatory, must contain only safe transport
characters, and must exactly match `adb get-serialno`. The transport must
report recovery mode, the debug shell must be uid 0, and the device tree must
contain `google,sargo` before any block read occurs.

The collector requires Python 3 and `adb`. Its only remote operations are:

- `get-state` and `get-serialno`;
- `id`, `uname`, and reads of device-tree model/compatibility;
- reads of the eMMC CID, logical sector size, and sysfs size (whose units are
  always 512-byte sectors, converted to logical-LBA count in the snapshot);
- bounded `dd if=/dev/mmcblk0` reads of GPT headers and entry arrays, with no
  `of=` argument; and
- reads of each discovered partition's sysfs `uevent`, `start`, and `size`.

It never invokes ADB push, mount, reboot, fastboot, LVM, a filesystem tool, or
a command with a block-device output path.

## Integrity checks

The raw parser validates:

1. exact byte counts for every bounded read;
2. GPT signatures, revision, header sizes, reciprocal header pointers, usable
   ranges, and header CRC32 values;
3. bounded entry count/size and the entry-array CRC32;
4. matching primary/backup geometry, disk GUID, entry shape, and entry bytes;
5. nonzero unique partition GUIDs, valid UTF-16 names, bounds, and no overlap;
6. exact PARTUUID, name, number, start, and length agreement with the kernel;
   and
7. the conjunction of ADB serial, `google,sargo`, and eMMC CID.

Two backup-table layouts are accepted. `independent` is the conventional entry
array immediately before the backup header. `aliases-primary` is accepted only
when the CRC-valid backup header points to the exact primary entry-array LBA.
Every other backup overlap or placement is rejected.

Frankensargo currently reports an all-zero GPT disk GUID and
`aliases-primary`. Neither is normalized away: the snapshot records both facts
and requires the serial/CID binding. The missing independent entry-array copy
is a recovery defect that must be covered by an off-device GPT capture before
any write is authorized.

## Canonical hash

The JSON object satisfies
[`frankensargo-inventory-v1.schema.json`](../schema/frankensargo-inventory-v1.schema.json).
That schema checks only JSON structure and local scalar constraints. It cannot
prove GPT arithmetic, uniqueness, CRCs, agreement with a live block device, or
the canonical hash. A consumer of stored or edited JSON must recompute the
hash and apply equivalent semantic and device-binding validation; a
schema-valid document alone is not verified evidence. The future manifest
builder must include such a semantic verifier.

To recompute `canonical_sha256`:

1. remove the top-level `canonical_sha256` member;
2. encode the remaining object as UTF-8 JSON with keys sorted
   lexicographically and no insignificant whitespace;
3. append one LF byte; and
4. hash those bytes with SHA-256 and prefix the lowercase digest with
   `sha256:`.

There is deliberately no capture timestamp, output filename, USB bus number,
or host name in the payload. Repeated reads of unchanged hardware therefore
produce identical JSON when the PocketBoot environment and requested ADB
serial are also unchanged. Kernel node names remain explicitly labelled as
observations.

Generated snapshots belong under ignored `out/inventory/`. They contain real
device identifiers and are evidence to review, not source-controlled blanket
permission to absorb a partition. A manifest builder must copy the verified
CID, disk GUID, type GUID, PARTUUID, geometry, and canonical snapshot hash into
the authorization scope and add content/LP/firmware evidence separately.
