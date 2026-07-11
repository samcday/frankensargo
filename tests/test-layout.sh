#!/bin/sh

set -eu

LC_ALL=C
export LC_ALL

TEST_DIR=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH='' cd -P -- "$TEST_DIR/.." && pwd)
PLANNER=$ROOT/target/plan-layout

fail()
{
	printf 'not ok - %s\n' "$*" >&2
	exit 1
}

assert_eq()
{
	assert_name=$1
	assert_expected=$2
	assert_actual=$3

	if [ "$assert_expected" != "$assert_actual" ]; then
		printf 'not ok - %s\nexpected:\n%s\nactual:\n%s\n' \
			"$assert_name" "$assert_expected" "$assert_actual" >&2
		exit 1
	fi
}

assert_fails()
{
	assert_name=$1
	shift
	if "$@" >/dev/null 2>&1; then
		fail "$assert_name unexpectedly succeeded"
	fi
}

plan_value()
{
	plan_key=$1
	printf '%s\n' "$plan" | sed -n "s/^${plan_key}=//p"
}

for command_name in mktemp rm sed sfdisk stat truncate; do
	command -v "$command_name" >/dev/null 2>&1 ||
		fail "required command is unavailable: $command_name"
done

[ -x "$PLANNER" ] || fail "planner is not executable: $PLANNER"

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/frankensargo-layout.XXXXXX")
trap 'rm -rf "$tmpdir"' 0 1 2 15

image=$tmpdir/userdata.img
truncate -s 4G "$image"

plan=$("$PLANNER" "$image")
expected_plan='LABEL=dos
SECTOR_SIZE=512
TOTAL_BYTES=4294967296
TOTAL_SECTORS=8388608
ALIGNMENT_SECTORS=2048
BOOT_SIZE_MIB=1024
BOOT_START=2048
BOOT_SIZE=2097152
BOOT_END_EXCLUSIVE=2099200
BOOT_TYPE=0x83
BOOT_ACTIVE=1
PV_START=2099200
PV_SIZE=6287360
PV_END_EXCLUSIVE=8386560
PV_TYPE=0x8e
TAIL_START=8386560
TAIL_SIZE=2048'
assert_eq 'default plan' "$expected_plan" "$plan"

json=$("$PLANNER" --json "$image")
expected_json='{
  "label": "dos",
  "sector_size": 512,
  "total_bytes": 4294967296,
  "total_sectors": 8388608,
  "alignment_sectors": 2048,
  "partitions": [
    {"number": 1, "start": 2048, "size": 2097152, "type": "0x83", "bootable": true},
    {"number": 2, "start": 2099200, "size": 6287360, "type": "0x8e", "bootable": false}
  ],
  "tail": {"start": 8386560, "size": 2048}
}'
assert_eq 'JSON plan' "$expected_json" "$json"

sfdisk_script=$("$PLANNER" --sfdisk "$image")
expected_sfdisk='label: dos
unit: sectors
sector-size: 512

start=2048, size=2097152, type=83, bootable
start=2099200, size=6287360, type=8e'
assert_eq 'sfdisk script' "$expected_sfdisk" "$sfdisk_script"

boot_start=$(plan_value BOOT_START)
boot_size=$(plan_value BOOT_SIZE)
boot_end=$(plan_value BOOT_END_EXCLUSIVE)
pv_start=$(plan_value PV_START)
pv_size=$(plan_value PV_SIZE)
pv_end=$(plan_value PV_END_EXCLUSIVE)
tail_start=$(plan_value TAIL_START)
tail_size=$(plan_value TAIL_SIZE)
total_sectors=$(plan_value TOTAL_SECTORS)

[ $((boot_start % 2048)) -eq 0 ] || fail 'boot start is not 1 MiB aligned'
[ $((pv_start % 2048)) -eq 0 ] || fail 'PV start is not 1 MiB aligned'
[ $((boot_start + boot_size)) -eq "$boot_end" ] || fail 'boot boundary is inconsistent'
[ "$boot_end" -eq "$pv_start" ] || fail 'boot and PV are not contiguous'
[ $((pv_start + pv_size)) -eq "$pv_end" ] || fail 'PV boundary is inconsistent'
[ "$pv_end" -eq "$tail_start" ] || fail 'PV and tail are not contiguous'
[ $((tail_start + tail_size)) -eq "$total_sectors" ] || fail 'tail does not reach target end'
[ "$tail_size" -eq 2048 ] || fail 'tail is not exactly 1 MiB'

script_file=$tmpdir/layout.sfdisk
printf '%s\n' "$sfdisk_script" >"$script_file"
sfdisk --quiet "$image" <"$script_file" >/dev/null

dump=$(sfdisk --dump "$image")
label_id=$(printf '%s\n' "$dump" | sed -n 's/^label-id: //p')
[ -n "$label_id" ] || fail 'sfdisk dump omitted the DOS label ID'
expected_dump="label: dos
label-id: $label_id
device: $image
unit: sectors
sector-size: 512

${image}1 : start=        2048, size=     2097152, type=83, bootable
${image}2 : start=     2099200, size=     6287360, type=8e"
assert_eq 'applied sfdisk dump' "$expected_dump" "$dump"

sfdisk_json=$(sfdisk --json "$image")
expected_sfdisk_json="{
   \"partitiontable\": {
      \"label\": \"dos\",
      \"id\": \"$label_id\",
      \"device\": \"$image\",
      \"unit\": \"sectors\",
      \"sectorsize\": 512,
      \"partitions\": [
         {
            \"node\": \"${image}1\",
            \"start\": 2048,
            \"size\": 2097152,
            \"type\": \"83\",
            \"bootable\": true
         },{
            \"node\": \"${image}2\",
            \"start\": 2099200,
            \"size\": 6287360,
            \"type\": \"8e\"
         }
      ]
   }
}"
assert_eq 'applied sfdisk JSON' "$expected_sfdisk_json" "$sfdisk_json"

small_image=$tmpdir/too-small.img
truncate -s 1026M "$small_image"
assert_fails 'minimum-size check' "$PLANNER" "$small_image"

minimum_image=$tmpdir/minimum.img
truncate -s 1027M "$minimum_image"
minimum_plan=$("$PLANNER" "$minimum_image")
minimum_pv_size=$(printf '%s\n' "$minimum_plan" | sed -n 's/^PV_SIZE=//p')
assert_eq 'minimum PV is exactly 1 MiB' '2048' "$minimum_pv_size"

odd_image=$tmpdir/odd-sized.img
truncate -s 1076888065 "$odd_image"
assert_fails 'sector-size check' "$PLANNER" --boot-mib 1 "$odd_image"

overflow_image=$tmpdir/overflow.img
truncate -s 2199023256064 "$overflow_image"
assert_fails 'MBR overflow check' "$PLANNER" "$overflow_image"

maximum_image=$tmpdir/maximum.img
truncate -s 2199023255552 "$maximum_image"
maximum_plan=$("$PLANNER" "$maximum_image")
maximum_total=$(printf '%s\n' "$maximum_plan" | sed -n 's/^TOTAL_SECTORS=//p')
assert_eq 'maximum MBR sector count' '4294967296' "$maximum_total"

assert_fails 'zero boot check' "$PLANNER" --boot-mib 0 "$image"
assert_fails 'non-numeric boot check' "$PLANNER" --boot-mib nope "$image"
assert_fails 'boot arithmetic overflow check' \
	"$PLANNER" --boot-mib 999999999999999999999 "$image"

printf 'ok - layout planner\n'
