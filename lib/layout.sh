#!/bin/sh

# Read-only layout calculations for an MBR nested inside Android userdata.

LAYOUT_SECTOR_SIZE=512
LAYOUT_ALIGNMENT_SECTORS=2048
# shellcheck disable=SC2034 # Public default consumed by target/plan-layout.
LAYOUT_DEFAULT_BOOT_MIB=1024
LAYOUT_MIN_PV_SECTORS=2048
LAYOUT_TAIL_SECTORS=2048
LAYOUT_MBR_MAX_SECTORS=4294967296
LAYOUT_MBR_MAX_BYTES=2199023255552
LAYOUT_MAX_BOOT_MIB=2097149

layout_error()
{
	printf 'plan-layout: %s\n' "$*" >&2
}

layout_normalize_uint()
{
	layout_uint=$1

	case $layout_uint in
		'' | *[!0-9]*)
			return 1
			;;
	esac

	while [ "${layout_uint#0}" != "$layout_uint" ]; do
		layout_uint=${layout_uint#0}
	done
	[ -n "$layout_uint" ] || layout_uint=0
	printf '%s\n' "$layout_uint"
}

# Both operands must already be normalized unsigned decimal integers. This
# length check prevents test(1) from receiving a value outside signed 64-bit.
layout_uint_le()
{
	layout_left=$1
	layout_right=$2

	if [ "${#layout_left}" -lt "${#layout_right}" ]; then
		return 0
	fi
	if [ "${#layout_left}" -gt "${#layout_right}" ]; then
		return 1
	fi
	[ "$layout_left" -le "$layout_right" ]
}

layout_target_bytes()
{
	layout_target=$1

	if [ -f "$layout_target" ]; then
		stat -Lc '%s' -- "$layout_target" 2>/dev/null || {
			layout_error "cannot inspect regular file: $layout_target"
			return 1
		}
		return 0
	fi

	if [ -b "$layout_target" ]; then
		if ! command -v blockdev >/dev/null 2>&1; then
			layout_error 'blockdev is required to inspect a block device'
			return 1
		fi
		blockdev --getsize64 "$layout_target" 2>/dev/null || {
			layout_error "cannot inspect block device: $layout_target"
			return 1
		}
		return 0
	fi

	layout_error "target is not a regular file or block device: $layout_target"
	return 1
}

# Populate LAYOUT_* result variables. No write-capable command is used here.
layout_plan()
{
	LAYOUT_TARGET=$1

	if ! LAYOUT_BOOT_MIB=$(layout_normalize_uint "$2"); then
		layout_error 'boot size must be a positive integer number of MiB'
		return 1
	fi
	if [ "$LAYOUT_BOOT_MIB" -eq 0 ]; then
		layout_error 'boot size must be at least 1 MiB'
		return 1
	fi
	if ! layout_uint_le "$LAYOUT_BOOT_MIB" "$LAYOUT_MAX_BOOT_MIB"; then
		layout_error "boot size exceeds MBR arithmetic limit ($LAYOUT_MAX_BOOT_MIB MiB)"
		return 1
	fi

	if ! LAYOUT_TOTAL_BYTES=$(layout_target_bytes "$LAYOUT_TARGET"); then
		return 1
	fi
	if ! LAYOUT_TOTAL_BYTES=$(layout_normalize_uint "$LAYOUT_TOTAL_BYTES"); then
		layout_error "target size is not an unsigned integer: $LAYOUT_TARGET"
		return 1
	fi
	if ! layout_uint_le "$LAYOUT_TOTAL_BYTES" "$LAYOUT_MBR_MAX_BYTES"; then
		layout_error 'target exceeds the 2 TiB limit of a 512-byte-sector MBR'
		return 1
	fi
	if [ $((LAYOUT_TOTAL_BYTES % LAYOUT_SECTOR_SIZE)) -ne 0 ]; then
		layout_error 'target size is not a multiple of the 512-byte sector size'
		return 1
	fi

	LAYOUT_TOTAL_SECTORS=$((LAYOUT_TOTAL_BYTES / LAYOUT_SECTOR_SIZE))
	LAYOUT_BOOT_START=$LAYOUT_ALIGNMENT_SECTORS
	LAYOUT_BOOT_SIZE=$((LAYOUT_BOOT_MIB * LAYOUT_ALIGNMENT_SECTORS))
	LAYOUT_BOOT_END_EXCLUSIVE=$((LAYOUT_BOOT_START + LAYOUT_BOOT_SIZE))
	LAYOUT_PV_START=$LAYOUT_BOOT_END_EXCLUSIVE
	LAYOUT_PV_END_EXCLUSIVE=$((LAYOUT_TOTAL_SECTORS - LAYOUT_TAIL_SECTORS))
	LAYOUT_PV_SIZE=$((LAYOUT_PV_END_EXCLUSIVE - LAYOUT_PV_START))
	LAYOUT_TAIL_START=$LAYOUT_PV_END_EXCLUSIVE

	if [ "$LAYOUT_TOTAL_SECTORS" -lt "$LAYOUT_TAIL_SECTORS" ] ||
		[ "$LAYOUT_PV_END_EXCLUSIVE" -lt "$LAYOUT_PV_START" ] ||
		[ "$LAYOUT_PV_SIZE" -lt "$LAYOUT_MIN_PV_SECTORS" ]; then
		layout_min_mib=$((LAYOUT_BOOT_MIB + 3))
		layout_error "target is too small; boot=${LAYOUT_BOOT_MIB} MiB requires at least ${layout_min_mib} MiB"
		return 1
	fi

	# These are invariants as well as guards against arithmetic or future
	# constant changes accidentally producing an invalid MBR plan.
	if [ $((LAYOUT_BOOT_START % LAYOUT_ALIGNMENT_SECTORS)) -ne 0 ] ||
		[ $((LAYOUT_PV_START % LAYOUT_ALIGNMENT_SECTORS)) -ne 0 ]; then
		layout_error 'internal error: partition start is not 1 MiB aligned'
		return 1
	fi
	if [ "$LAYOUT_BOOT_SIZE" -gt 4294967295 ] ||
		[ "$LAYOUT_PV_START" -gt 4294967295 ] ||
		[ "$LAYOUT_PV_SIZE" -gt 4294967295 ] ||
		[ "$LAYOUT_PV_END_EXCLUSIVE" -gt "$LAYOUT_MBR_MAX_SECTORS" ]; then
		layout_error 'calculated partition does not fit in 32-bit MBR fields'
		return 1
	fi
}

layout_print_env()
{
	printf '%s\n' \
		'LABEL=dos' \
		"SECTOR_SIZE=$LAYOUT_SECTOR_SIZE" \
		"TOTAL_BYTES=$LAYOUT_TOTAL_BYTES" \
		"TOTAL_SECTORS=$LAYOUT_TOTAL_SECTORS" \
		"ALIGNMENT_SECTORS=$LAYOUT_ALIGNMENT_SECTORS" \
		"BOOT_SIZE_MIB=$LAYOUT_BOOT_MIB" \
		"BOOT_START=$LAYOUT_BOOT_START" \
		"BOOT_SIZE=$LAYOUT_BOOT_SIZE" \
		"BOOT_END_EXCLUSIVE=$LAYOUT_BOOT_END_EXCLUSIVE" \
		'BOOT_TYPE=0x83' \
		'BOOT_ACTIVE=1' \
		"PV_START=$LAYOUT_PV_START" \
		"PV_SIZE=$LAYOUT_PV_SIZE" \
		"PV_END_EXCLUSIVE=$LAYOUT_PV_END_EXCLUSIVE" \
		'PV_TYPE=0x8e' \
		"TAIL_START=$LAYOUT_TAIL_START" \
		"TAIL_SIZE=$LAYOUT_TAIL_SECTORS"
}

layout_print_json()
{
	printf '%s\n' \
		'{' \
		'  "label": "dos",' \
		"  \"sector_size\": $LAYOUT_SECTOR_SIZE," \
		"  \"total_bytes\": $LAYOUT_TOTAL_BYTES," \
		"  \"total_sectors\": $LAYOUT_TOTAL_SECTORS," \
		"  \"alignment_sectors\": $LAYOUT_ALIGNMENT_SECTORS," \
		'  "partitions": [' \
		"    {\"number\": 1, \"start\": $LAYOUT_BOOT_START, \"size\": $LAYOUT_BOOT_SIZE, \"type\": \"0x83\", \"bootable\": true}," \
		"    {\"number\": 2, \"start\": $LAYOUT_PV_START, \"size\": $LAYOUT_PV_SIZE, \"type\": \"0x8e\", \"bootable\": false}" \
		'  ],' \
		"  \"tail\": {\"start\": $LAYOUT_TAIL_START, \"size\": $LAYOUT_TAIL_SECTORS}" \
		'}'
}

layout_print_sfdisk()
{
	printf '%s\n' \
		'label: dos' \
		'unit: sectors' \
		"sector-size: $LAYOUT_SECTOR_SIZE" \
		'' \
		"start=$LAYOUT_BOOT_START, size=$LAYOUT_BOOT_SIZE, type=83, bootable" \
		"start=$LAYOUT_PV_START, size=$LAYOUT_PV_SIZE, type=8e"
}
