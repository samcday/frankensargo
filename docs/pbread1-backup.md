# Resumable PBREAD1 userdata backup

`bin/backup-pbread1` makes the mandatory off-device, byte-for-byte copy before
`userdata` is allowed to become the anchor PV. It is a host-side controller:
it writes only beneath its selected run directory and asks PocketBoot only for
bounded reads and a final whole-source hash.

This is deliberately separate from the takeover writer. A completed backup is
evidence for a later authorization; it does not authorize `pvcreate` or any
other phone write.

## Prerequisites

- Boot the PBREAD1-capable PocketBoot image transiently.
- Use Fedora's packaged `fastboot`; on the Deck this is available inside
  `fedora-latest`.
- Supply a canonical inventory from `bin/inventory-pocketboot` whose identity
  still matches frankensargo.
- Retain the exact PocketBoot image so its SHA-256 is bound into the run.
- Keep at least two userdata sizes plus working reserve free. The controller
  retains verified chunks until the assembled raw image is independently
  verified, and quarantines inconsistent files rather than overwriting them.
- Use a private run path. The controller creates and requires the run directory
  and every subdirectory at mode `0700`; locks, manifests, chunks and the raw
  image are `0600`. It rejects an existing permissive directory or lock instead
  of silently publishing userdata through the host's umask.

The current exact userdata geometry is 53,648,801,280 bytes, so the fixed
64 MiB plan has 799 full chunks and one final chunk: 800 chunks total. The
last range begins at `0xc7c000000` and is `0x1b7be00` bytes long.

## Plan without USB or writes

Run this inside `fedora-latest`:

```sh
cd ~/src/frankensargo
bin/backup-pbread1 backup \
  --dry-run \
  --run-dir /home/deck/frankensargo-backup/2026-07-12/pbread1-userdata \
  --inventory out/inventory/frankensargo.json \
  --serial 99NAY1AZG1 \
  --partuuid db04e713-11c3-4d68-bec2-8cc483bd3891 \
  --pocketboot-image out/pocketboot/pocketboot-sargo-lab.img
```

Dry-run validates and hashes the supplied inventory and PocketBoot image, then
prints the proposed immutable manifest. It does not create the run directory,
enumerate USB, or execute `fastboot`.

For a complete offline exercise, add `--offline-source FILE`. The file must
have exactly the partition size recorded by the inventory. A non-default
`--chunk-bytes` is accepted only with `--offline-source`, which lets the test
suite exercise the full workflow with small ordinary files.

## Capture or resume

Remove `--dry-run` and repeat the otherwise identical command. The controller
always invokes fastboot with the explicit serial. Its device command set is
limited to:

```text
getvar:serialno
getvar:product
getvar:compatible
getvar:partition-size:userdata
oem read <partuuid32> <offset_hex> <length_hex>
oem hash <partuuid32>
get_staged <host-file>
```

It never invokes `flash`, `erase`, `format`, `stage`, `download`, `reboot`,
`continue`, a slot command, or a shell.

Each `oem read` stages a 512-byte PBREAD1 header followed by at most 64 MiB of
raw data. The header binds the full PARTUUID, GPT type, label, observed kernel
name, start LBA, sector count, sector size, partition byte size, requested
offset/length, and the device's SHA-256 of those bytes. The host validates all
fields, exact envelope length, and an independent payload SHA-256 before it
atomically publishes a chunk.

The on-disk run contains:

```text
manifest.json              immutable device, inventory, partition, image and plan binding
manifest.json.sha256       exact manifest checksum
journal.json               atomically replaced completion journal
chunks/00000000.bin        independently revalidated raw chunks
rejected/                  inconsistent prior files retained for inspection
userdata.raw               assembled byte-for-byte image
userdata.raw.sha256        fresh full destination read hash
```

After a cable failure, PocketBoot reboot, host restart, or battery interruption,
run the same command again. Resume first checks the manifest checksum and exact
input binding, then rereads every purportedly complete chunk and compares it
with its journaled length and SHA-256. An unjournaled or inconsistent file is
quarantined and recaptured. No partial download is promoted.

After all chunks are present, the controller assembles them in offset order,
flushes the output, and hashes the complete raw image in a fresh read. It then
asks PocketBoot's `oem hash` command to read and hash the complete source again.
The run succeeds only when the source and destination SHA-256 values match.

## Verify and destructive-operation gate

Verification is host-only and never invokes fastboot:

```sh
bin/backup-pbread1 verify \
  --run-dir /home/deck/frankensargo-backup/2026-07-12/pbread1-userdata
```

It rechecks the manifest checksum, every raw chunk, the assembled file, and the
recorded source/destination match. Immediately before the separately reviewed
userdata bootstrap writer is allowed to run, repeat the original `backup`
command once more. A complete run skips data capture but revalidates all host
files and performs another full device-side source hash. Any mismatch stops
the takeover.

## PBREAD1 envelope v1

All integers are little-endian and all unused bytes must be zero.

| Offset | Field |
|---:|---|
| `0x000` | `PBREAD1\0` magic, 8 bytes |
| `0x008` | header length `512`, `u32` |
| `0x00c` | flags: `1` payload, `2` whole-source hash record |
| `0x010` | PARTUUID, 32 lowercase ASCII hex bytes |
| `0x030` | GPT type GUID, 32 lowercase ASCII hex bytes |
| `0x050` | start LBA, `u64` |
| `0x058` | sector count, `u64` |
| `0x060` | logical sector bytes, `u32` |
| `0x064` | reserved zero, `u32` |
| `0x068` | partition bytes, `u64` |
| `0x070` | source offset bytes, `u64` |
| `0x078` | source length bytes, `u64` |
| `0x080` | staged payload bytes, `u64` |
| `0x088` | source-range SHA-256, 32 raw bytes |
| `0x0a8` | NUL-padded ASCII partlabel, 64 bytes |
| `0x0e8` | NUL-padded ASCII kernel name, 32 bytes |
| `0x108` | zero-filled reserved tail through byte 511 |

The whole-source hash record has flag `2`, offset zero, source length equal to
the complete partition, and no payload. Its staged file is exactly 512 bytes.
