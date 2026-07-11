#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
COLLECTOR=$ROOT/bin/inventory-pocketboot
FAKE_ADB=$ROOT/tests/helpers/fake-inventory-adb.py
SCHEMA=$ROOT/schema/frankensargo-inventory-v1.schema.json

fail() {
	echo "inventory collector test: $*" >&2
	exit 1
}

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/frankensargo-inventory.XXXXXX")
trap 'rm -rf "$tmpdir"' 0 1 2 15
mkdir "$tmpdir/bin"
ln -s "$FAKE_ADB" "$tmpdir/bin/adb"
ADB=$tmpdir/bin/adb
ADB_TEST_LOG=$tmpdir/adb.log
export ADB ADB_TEST_LOG
PATH=$tmpdir/bin:$PATH
export PATH

"$COLLECTOR" --help >/dev/null
if "$COLLECTOR" >/dev/null 2>&1; then
	fail 'missing serial unexpectedly succeeded'
fi
if "$COLLECTOR" --serial=-unsafe >/dev/null 2>&1; then
	fail 'option-like serial unexpectedly succeeded'
fi

: >"$ADB_TEST_LOG"
"$COLLECTOR" --serial TEST-SARGO >"$tmpdir/inventory-a.json"
"$COLLECTOR" --serial TEST-SARGO >"$tmpdir/inventory-b.json"
cmp "$tmpdir/inventory-a.json" "$tmpdir/inventory-b.json" >/dev/null ||
	fail 'identical hardware did not produce deterministic JSON'
jsonschema "$SCHEMA" -i "$tmpdir/inventory-a.json" >/dev/null 2>&1 ||
	fail 'inventory does not satisfy its schema'

[ "$(jq -r '.schema' "$tmpdir/inventory-a.json")" = \
	'org.frankensargo.inventory/1' ] || fail 'schema identifier mismatch'
[ "$(jq -r '.device.adb_serial' "$tmpdir/inventory-a.json")" = TEST-SARGO ] ||
	fail 'serial missing from inventory'
[ "$(jq -r '.device.emmc.cid' "$tmpdir/inventory-a.json")" = \
	13014e53304a394b381011182ce76600 ] || fail 'CID mismatch'
[ "$(jq -r '.gpt.disk_guid' "$tmpdir/inventory-a.json")" = \
	00000000-0000-0000-0000-000000000000 ] || fail 'zero disk GUID mismatch'
[ "$(jq -r '.gpt.disk_guid_is_zero' "$tmpdir/inventory-a.json")" = true ] ||
	fail 'zero disk GUID flag mismatch'
[ "$(jq -r '.gpt.backup_entry_array_layout' "$tmpdir/inventory-a.json")" = \
	independent ] || fail 'standard backup layout mismatch'
[ "$(jq -r '.gpt.backup_entry_array_independent' "$tmpdir/inventory-a.json")" = \
	true ] || fail 'standard backup independence mismatch'
[ "$(jq -r '.gpt.partitions | length' "$tmpdir/inventory-a.json")" -eq 2 ] ||
	fail 'used partition count mismatch'
[ "$(jq -r '.gpt.partitions[0].type_guid' "$tmpdir/inventory-a.json")" = \
	97d7b011-54da-4835-b3c4-917ad6e73d74 ] || fail 'type GUID decoding mismatch'

expected_hash=$(jq -cS 'del(.canonical_sha256)' "$tmpdir/inventory-a.json" |
	sha256sum)
expected_hash=sha256:${expected_hash%% *}
[ "$(jq -r '.canonical_sha256' "$tmpdir/inventory-a.json")" = \
	"$expected_hash" ] || fail 'canonical inventory hash mismatch'

ADB_TEST_ALIAS_BACKUP=1 "$COLLECTOR" --serial TEST-SARGO \
	>"$tmpdir/alias.json"
[ "$(jq -r '.gpt.backup_entry_array_layout' "$tmpdir/alias.json")" = \
	aliases-primary ] || fail 'aliased backup layout was not recorded'
[ "$(jq -r '.gpt.backup_entry_array_independent' "$tmpdir/alias.json")" = \
	false ] || fail 'aliased backup was incorrectly marked independent'
jsonschema "$SCHEMA" -i "$tmpdir/alias.json" >/dev/null 2>&1 ||
	fail 'aliased inventory does not satisfy its schema'

ADB_TEST_SECTOR_SIZE=4096 "$COLLECTOR" --serial TEST-SARGO \
	>"$tmpdir/sector-4096.json"
[ "$(jq -r '.device.emmc.logical_sector_size' "$tmpdir/sector-4096.json")" \
	-eq 4096 ] || fail '4096-byte logical sector size was not recorded'
[ "$(jq -r '.device.emmc.sector_count' "$tmpdir/sector-4096.json")" -eq \
	"$((4096))" ] || fail 'sysfs 512-sector size was not converted to GPT LBAs'
[ "$(jq -r '.device.emmc.size_bytes' "$tmpdir/sector-4096.json")" -eq \
	"$((4096 * 4096))" ] || fail '4096-byte disk size calculation mismatch'
[ "$(jq -r '.gpt.partitions[0].byte_size' "$tmpdir/sector-4096.json")" -eq \
	"$((100 * 4096))" ] || fail '4096-byte partition size calculation mismatch'
jsonschema "$SCHEMA" -i "$tmpdir/sector-4096.json" >/dev/null 2>&1 ||
	fail '4096-byte-sector inventory does not satisfy its schema'

jq '.gpt.backup_entry_array_independent = false' "$tmpdir/inventory-a.json" \
	>"$tmpdir/bad-layout-flag.json"
if jsonschema "$SCHEMA" -i "$tmpdir/bad-layout-flag.json" >/dev/null 2>&1; then
	fail 'schema accepted an inconsistent backup-layout flag'
fi
jq '.gpt.disk_guid_is_zero = false' "$tmpdir/inventory-a.json" \
	>"$tmpdir/bad-zero-flag.json"
if jsonschema "$SCHEMA" -i "$tmpdir/bad-zero-flag.json" >/dev/null 2>&1; then
	fail 'schema accepted an inconsistent zero-GUID flag'
fi

if ADB_TEST_UNSAFE_BACKUP=1 "$COLLECTOR" --serial TEST-SARGO \
	>"$tmpdir/unsafe.out" 2>"$tmpdir/unsafe.err"; then
	fail 'unsafe backup GPT placement unexpectedly succeeded'
fi
grep -Fq 'backup GPT entry array has an unsafe layout' "$tmpdir/unsafe.err" ||
	fail 'unsafe backup-layout diagnostic is missing'

if ADB_TEST_UNSAFE_BACKUP_HEADER=1 "$COLLECTOR" --serial TEST-SARGO \
	>"$tmpdir/header-placement.out" 2>"$tmpdir/header-placement.err"; then
	fail 'backup GPT header inside the usable range unexpectedly succeeded'
fi
grep -Fq 'backup GPT header is not at the final logical LBA' \
	"$tmpdir/header-placement.err" ||
	fail 'unsafe backup-header diagnostic is missing'

if ADB_TEST_CORRUPT_PRIMARY_HEADER=1 "$COLLECTOR" --serial TEST-SARGO \
	>"$tmpdir/corrupt.out" 2>"$tmpdir/corrupt.err"; then
	fail 'corrupt primary GPT unexpectedly succeeded'
fi
grep -Fq 'primary GPT header CRC mismatch' "$tmpdir/corrupt.err" ||
	fail 'corrupt GPT diagnostic is missing'

if ADB_TEST_MISMATCH_BACKUP=1 "$COLLECTOR" --serial TEST-SARGO \
	>"$tmpdir/backup.out" 2>"$tmpdir/backup.err"; then
	fail 'mismatched backup GPT unexpectedly succeeded'
fi
grep -Fq 'primary/backup GPT mismatch in disk_guid' "$tmpdir/backup.err" ||
	fail 'backup mismatch diagnostic is missing'

if ADB_TEST_SYSFS_MISMATCH=1 "$COLLECTOR" --serial TEST-SARGO \
	>"$tmpdir/sysfs.out" 2>"$tmpdir/sysfs.err"; then
	fail 'kernel/GPT mismatch unexpectedly succeeded'
fi
grep -Fq 'kernel/GPT mismatch' "$tmpdir/sysfs.err" ||
	fail 'kernel mismatch diagnostic is missing'

if ADB_TEST_SHORT_READ=1 "$COLLECTOR" --serial TEST-SARGO \
	>"$tmpdir/short.out" 2>"$tmpdir/short.err"; then
	fail 'short GPT read unexpectedly succeeded'
fi
grep -Fq 'short or contaminated GPT read' "$tmpdir/short.err" ||
	fail 'short-read diagnostic is missing'

cp "$ADB_TEST_LOG" "$tmpdir/safety.log"
: >"$ADB_TEST_LOG"
if ADB_TEST_REPORTED_SERIAL=OTHER-SARGO "$COLLECTOR" --serial TEST-SARGO \
	>"$tmpdir/serial.out" 2>"$tmpdir/serial.err"; then
	fail 'mismatched ADB serial unexpectedly succeeded'
fi
[ "$(wc -l <"$ADB_TEST_LOG")" -eq 2 ] ||
	fail 'serial mismatch did not stop after two identity calls'
grep -Fq 'ADB serial does not match requested serial' "$tmpdir/serial.err" ||
	fail 'serial mismatch diagnostic is missing'

if grep -Ev '^-s TEST-SARGO ' "$tmpdir/safety.log" >/dev/null; then
	fail 'an unscoped ADB command was observed'
fi
if grep -Ev '^-s TEST-SARGO (get-state|get-serialno|shell /usr/bin/id|shell /bin/uname -r|shell /bin/cat /proc/device-tree/(compatible|model)|shell /bin/cat /sys/block/mmcblk0/device/cid|shell /bin/cat /sys/class/block/mmcblk0/(queue/logical_block_size|size)|shell /bin/cat( /sys/class/block/mmcblk0p[0-9]+/(uevent|start|size))+|exec-out /bin/sh -c exec /bin/dd if=/dev/mmcblk0 bs=(512|1024|2048|4096) skip=[0-9]+ count=[0-9]+ 2>/dev/null)$' \
	"$tmpdir/safety.log" >/dev/null; then
	fail 'a command outside the read-only ADB allowlist was observed'
fi

echo 'inventory collector tests: PASS'
