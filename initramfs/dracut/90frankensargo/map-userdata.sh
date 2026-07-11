#!/bin/sh

# Validate the nested MBR in Android userdata and expose its first two entries
# through stable device-mapper names.  This script deliberately contains no
# partitioning, pvcreate, filesystem, or other write-to-media operation.

set -f

program=${0##*/}
boot_name=franken-boot
pv_name=franken-userdata-pv

print_tables=0
hook_mode=0
config_path=${FRANKEN_CONFIG:-/etc/frankensargo-map.conf}
config_explicit=0

cli_device=
cli_boot_start=
cli_boot_sectors=
cli_pv_start=
cli_pv_sectors=

env_device=${FRANKEN_USERDATA:-${FRANKEN_USERDATA_DEVICE:-}}
env_boot_start=${FRANKEN_BOOT_START:-}
env_boot_sectors=${FRANKEN_BOOT_SECTORS:-}
env_pv_start=${FRANKEN_PV_START:-}
env_pv_sectors=${FRANKEN_PV_SECTORS:-}

usage() {
    cat <<EOF
Usage: $program [--hook] [--print-tables] [OPTIONS]

Options:
  --device PATH       containing Android userdata block device
  --boot-start N      expected MBR entry 1 start, in 512-byte sectors
  --boot-sectors N    expected MBR entry 1 length, in 512-byte sectors
  --pv-start N        expected MBR entry 2 start, in 512-byte sectors
  --pv-sectors N      expected MBR entry 2 length, in 512-byte sectors
  --config PATH       fallback environment file
  --print-tables      validate and print tables without calling dmsetup
  --hook              wait quietly when userdata is not present yet

Kernel command line keys are franken.userdata, franken.boot_start,
franken.boot_sectors, franken.pv_start, and franken.pv_sectors.  Equivalent
FRANKEN_* values may be placed in /etc/frankensargo-map.conf.
EOF
}

log() {
    printf '%s: %s\n' "$program" "$*" >&2
}

fail() {
    log "error: $*"
    exit 1
}

need_option_value() {
    [ "$#" -ge 2 ] || fail "$1 requires a value"
    [ -n "$2" ] || fail "$1 requires a non-empty value"
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --device)
            need_option_value "$@"
            cli_device=$2
            shift 2
            ;;
        --boot-start)
            need_option_value "$@"
            cli_boot_start=$2
            shift 2
            ;;
        --boot-sectors)
            need_option_value "$@"
            cli_boot_sectors=$2
            shift 2
            ;;
        --pv-start)
            need_option_value "$@"
            cli_pv_start=$2
            shift 2
            ;;
        --pv-sectors)
            need_option_value "$@"
            cli_pv_sectors=$2
            shift 2
            ;;
        --config)
            need_option_value "$@"
            config_path=$2
            config_explicit=1
            shift 2
            ;;
        --print-tables|--dry-run)
            print_tables=1
            shift
            ;;
        --hook)
            hook_mode=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            [ "$#" -eq 0 ] || fail "unexpected positional argument: $1"
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

config_device=
config_boot_start=
config_boot_sectors=
config_pv_start=
config_pv_sectors=

if [ -r "$config_path" ]; then
    while IFS= read -r config_line || [ -n "$config_line" ]; do
        case $config_line in
            ''|'#'*)
                continue
                ;;
            'export '*)
                config_line=${config_line#export }
                ;;
        esac

        config_key=${config_line%%=*}
        config_value=${config_line#*=}
        [ "$config_key" != "$config_line" ] || continue

        case $config_value in
            \"*\")
                config_value=${config_value#\"}
                config_value=${config_value%\"}
                ;;
            \'*\')
                config_value=${config_value#\'}
                config_value=${config_value%\'}
                ;;
        esac

        case $config_key in
            FRANKEN_USERDATA|FRANKEN_USERDATA_DEVICE)
                config_device=$config_value
                ;;
            FRANKEN_BOOT_START)
                config_boot_start=$config_value
                ;;
            FRANKEN_BOOT_SECTORS)
                config_boot_sectors=$config_value
                ;;
            FRANKEN_PV_START)
                config_pv_start=$config_value
                ;;
            FRANKEN_PV_SECTORS)
                config_pv_sectors=$config_value
                ;;
        esac
    done < "$config_path"
elif [ "$config_explicit" -eq 1 ]; then
    fail "cannot read config: $config_path"
fi

cmd_device=
cmd_boot_start=
cmd_boot_sectors=
cmd_pv_start=
cmd_pv_sectors=

if [ "${FRANKEN_CMDLINE+x}" = x ]; then
    kernel_cmdline=$FRANKEN_CMDLINE
elif [ -r /proc/cmdline ]; then
    IFS= read -r kernel_cmdline < /proc/cmdline || kernel_cmdline=
else
    kernel_cmdline=
fi

for cmdline_word in $kernel_cmdline; do
    case $cmdline_word in
        franken.userdata=*)
            cmd_device=${cmdline_word#franken.userdata=}
            ;;
        franken.boot_start=*)
            cmd_boot_start=${cmdline_word#franken.boot_start=}
            ;;
        franken.boot_sectors=*)
            cmd_boot_sectors=${cmdline_word#franken.boot_sectors=}
            ;;
        franken.pv_start=*)
            cmd_pv_start=${cmdline_word#franken.pv_start=}
            ;;
        franken.pv_sectors=*)
            cmd_pv_sectors=${cmdline_word#franken.pv_sectors=}
            ;;
    esac
done

device=$config_device
boot_start=$config_boot_start
boot_sectors=$config_boot_sectors
pv_start=$config_pv_start
pv_sectors=$config_pv_sectors

[ -n "$env_device" ] && device=$env_device
[ -n "$env_boot_start" ] && boot_start=$env_boot_start
[ -n "$env_boot_sectors" ] && boot_sectors=$env_boot_sectors
[ -n "$env_pv_start" ] && pv_start=$env_pv_start
[ -n "$env_pv_sectors" ] && pv_sectors=$env_pv_sectors

[ -n "$cmd_device" ] && device=$cmd_device
[ -n "$cmd_boot_start" ] && boot_start=$cmd_boot_start
[ -n "$cmd_boot_sectors" ] && boot_sectors=$cmd_boot_sectors
[ -n "$cmd_pv_start" ] && pv_start=$cmd_pv_start
[ -n "$cmd_pv_sectors" ] && pv_sectors=$cmd_pv_sectors

[ -n "$cli_device" ] && device=$cli_device
[ -n "$cli_boot_start" ] && boot_start=$cli_boot_start
[ -n "$cli_boot_sectors" ] && boot_sectors=$cli_boot_sectors
[ -n "$cli_pv_start" ] && pv_start=$cli_pv_start
[ -n "$cli_pv_sectors" ] && pv_sectors=$cli_pv_sectors

if [ -z "$boot_start$boot_sectors$pv_start$pv_sectors" ]; then
    [ "$hook_mode" -eq 1 ] && exit 0
    fail "nested MBR geometry is not configured"
fi

[ -n "$boot_start" ] || fail "missing franken.boot_start"
[ -n "$boot_sectors" ] || fail "missing franken.boot_sectors"
[ -n "$pv_start" ] || fail "missing franken.pv_start"
[ -n "$pv_sectors" ] || fail "missing franken.pv_sectors"

if [ -z "$device" ]; then
    for candidate in \
        /dev/disk/by-partlabel/userdata \
        /dev/block/by-name/userdata; do
        if [ -e "$candidate" ]; then
            device=$candidate
            break
        fi
    done
fi

if [ -z "$device" ] || [ ! -e "$device" ]; then
    [ "$hook_mode" -eq 1 ] && exit 0
    fail "userdata device is not present: ${device:-not specified}"
fi

case $device in
    *[[:space:]]*)
        fail "userdata device path contains whitespace: $device"
        ;;
esac

normalize_mbr_value() {
    normalize_label=$1
    normalize_value=$2

    case $normalize_value in
        ''|*[!0-9]*)
            fail "$normalize_label is not an unsigned decimal integer: $normalize_value"
            ;;
    esac

    while [ "${normalize_value#0}" != "$normalize_value" ]; do
        normalize_value=${normalize_value#0}
    done
    [ -n "$normalize_value" ] || normalize_value=0

    if [ "${#normalize_value}" -gt 10 ] \
        || { [ "${#normalize_value}" -eq 10 ] \
            && [ "$normalize_value" -gt 4294967295 ]; }; then
        fail "$normalize_label exceeds the MBR 32-bit field: $normalize_value"
    fi

    normalized_value=$normalize_value
}

normalize_mbr_value franken.boot_start "$boot_start"
boot_start=$normalized_value
normalize_mbr_value franken.boot_sectors "$boot_sectors"
boot_sectors=$normalized_value
normalize_mbr_value franken.pv_start "$pv_start"
pv_start=$normalized_value
normalize_mbr_value franken.pv_sectors "$pv_sectors"
pv_sectors=$normalized_value

[ "$boot_start" -gt 0 ] || fail "franken.boot_start must be greater than zero"
[ "$boot_sectors" -gt 0 ] || fail "franken.boot_sectors must be greater than zero"
[ "$pv_start" -gt 0 ] || fail "franken.pv_start must be greater than zero"
[ "$pv_sectors" -gt 0 ] || fail "franken.pv_sectors must be greater than zero"

if [ -b "$device" ]; then
    device_sectors=$(blockdev --getsz "$device" 2>/dev/null) \
        || fail "cannot determine size of $device"
elif [ "$print_tables" -eq 1 ] && [ -f "$device" ]; then
    device_bytes=$(wc -c < "$device") \
        || fail "cannot determine size of $device"
    # shellcheck disable=SC2086 # Collapse wc padding and reject extra fields.
    set -- $device_bytes
    [ "$#" -eq 1 ] || fail "invalid byte size reported for $device"
    device_bytes=$1
    case $device_bytes in
        ''|*[!0-9]*)
            fail "invalid byte size reported for $device: $device_bytes"
            ;;
    esac
    [ $((device_bytes % 512)) -eq 0 ] \
        || fail "regular-file fixture size is not a multiple of 512 bytes"
    device_sectors=$((device_bytes / 512))
else
    fail "userdata is not a block device (regular files require --print-tables): $device"
fi

# shellcheck disable=SC2086 # Collapse command padding and reject extra fields.
set -- $device_sectors
[ "$#" -eq 1 ] || fail "invalid sector count reported for $device"
device_sectors=$1
case $device_sectors in
    ''|*[!0-9]*)
        fail "invalid sector count reported for $device: $device_sectors"
        ;;
esac
[ "$device_sectors" -gt 0 ] || fail "userdata device is empty: $device"

read_byte_at() {
    od_output=$(od -An -tu1 -j "$1" -N 1 "$device" 2>/dev/null) \
        || fail "cannot read $2 from $device"
    # shellcheck disable=SC2086 # od returns whitespace-separated byte fields.
    set -- $od_output
    [ "$#" -eq 1 ] || fail "short read while reading $2 from $device"
    case $1 in
        ''|*[!0-9]*)
            fail "invalid byte while reading $2 from $device"
            ;;
    esac
    [ "$1" -le 255 ] || fail "invalid byte while reading $2 from $device"
    byte_value=$1
}

read_le32_at() {
    od_output=$(od -An -tu1 -j "$1" -N 4 "$device" 2>/dev/null) \
        || fail "cannot read $2 from $device"
    # shellcheck disable=SC2086 # od returns whitespace-separated byte fields.
    set -- $od_output
    [ "$#" -eq 4 ] || fail "short read while reading $2 from $device"
    for le_byte in "$@"; do
        case $le_byte in
            ''|*[!0-9]*)
                fail "invalid byte while reading $2 from $device"
                ;;
        esac
        [ "$le_byte" -le 255 ] \
            || fail "invalid byte while reading $2 from $device"
    done
    le32_value=$(($1 + ($2 * 256) + ($3 * 65536) + ($4 * 16777216)))
}

read_byte_at 510 'MBR signature byte 1'
signature_1=$byte_value
read_byte_at 511 'MBR signature byte 2'
signature_2=$byte_value
[ "$signature_1" -eq 85 ] && [ "$signature_2" -eq 170 ] \
    || fail "invalid MBR signature (expected 0x55aa)"

read_byte_at 446 'MBR entry 1 status'
entry1_status=$byte_value
read_byte_at 450 'MBR entry 1 type'
entry1_type=$byte_value
read_le32_at 454 'MBR entry 1 start'
entry1_start=$le32_value
read_le32_at 458 'MBR entry 1 size'
entry1_sectors=$le32_value

read_byte_at 462 'MBR entry 2 status'
entry2_status=$byte_value
read_byte_at 466 'MBR entry 2 type'
entry2_type=$byte_value
read_le32_at 470 'MBR entry 2 start'
entry2_start=$le32_value
read_le32_at 474 'MBR entry 2 size'
entry2_sectors=$le32_value

[ "$entry1_status" -eq 128 ] \
    || fail "MBR entry 1 status is not active (expected 0x80)"
[ "$entry1_type" -eq 131 ] \
    || fail "MBR entry 1 type is not Linux (expected 0x83)"
[ "$entry2_status" -eq 0 ] \
    || fail "MBR entry 2 status is not inactive (expected 0x00)"
[ "$entry2_type" -eq 142 ] \
    || fail "MBR entry 2 type is not Linux LVM (expected 0x8e)"

[ "$entry1_start" -eq "$boot_start" ] \
    || fail "MBR entry 1 start $entry1_start does not match expected $boot_start"
[ "$entry1_sectors" -eq "$boot_sectors" ] \
    || fail "MBR entry 1 size $entry1_sectors does not match expected $boot_sectors"
[ "$entry2_start" -eq "$pv_start" ] \
    || fail "MBR entry 2 start $entry2_start does not match expected $pv_start"
[ "$entry2_sectors" -eq "$pv_sectors" ] \
    || fail "MBR entry 2 size $entry2_sectors does not match expected $pv_sectors"

boot_end=$((boot_start + boot_sectors))
pv_end=$((pv_start + pv_sectors))

[ "$boot_end" -le "$device_sectors" ] \
    || fail "MBR entry 1 exceeds userdata ($boot_end > $device_sectors sectors)"
[ "$pv_end" -le "$device_sectors" ] \
    || fail "MBR entry 2 exceeds userdata ($pv_end > $device_sectors sectors)"

if [ "$boot_end" -gt "$pv_start" ] && [ "$pv_end" -gt "$boot_start" ]; then
    fail "MBR entries 1 and 2 overlap"
fi

boot_table="0 $boot_sectors linear $device $boot_start"
pv_table="0 $pv_sectors linear $device $pv_start"

if [ "$print_tables" -eq 1 ]; then
    printf '%s: %s\n' "$boot_name" "$boot_table"
    printf '%s: %s\n' "$pv_name" "$pv_table"
    exit 0
fi

udev_properties=$(udevadm info --query=property --name="$device" 2>/dev/null) \
    || fail "cannot identify backing device: $device"
device_major=
device_minor=
while IFS='=' read -r property_name property_value; do
    case $property_name in
        MAJOR)
            device_major=$property_value
            ;;
        MINOR)
            device_minor=$property_value
            ;;
    esac
done <<EOF
$udev_properties
EOF
case $device_major in
    ''|*[!0-9]*)
        fail "udev did not report a valid device number for $device"
        ;;
esac
case $device_minor in
    ''|*[!0-9]*)
        fail "udev did not report a valid device number for $device"
        ;;
esac
backing_devno=$device_major:$device_minor

mapping_matches() {
    match_name=$1
    match_sectors=$2
    match_start=$3
    current_table=$(dmsetup table "$match_name" 2>/dev/null) || return 1
    # shellcheck disable=SC2086 # dmsetup returns five table fields here.
    set -- $current_table
    [ "$#" -eq 5 ] || return 1
    [ "$1" -eq 0 ] 2>/dev/null || return 1
    [ "$2" -eq "$match_sectors" ] 2>/dev/null || return 1
    [ "$3" = linear ] || return 1
    [ "$4" = "$backing_devno" ] || return 1
    [ "$5" -eq "$match_start" ] 2>/dev/null || return 1
}

ensure_mapping() {
    mapping_name=$1
    mapping_sectors=$2
    mapping_start=$3
    mapping_table=$4

    if dmsetup info "$mapping_name" >/dev/null 2>&1; then
        if mapping_matches "$mapping_name" "$mapping_sectors" "$mapping_start"; then
            log "$mapping_name already exists with the requested extent"
            return 0
        fi
        fail "$mapping_name already exists with a different table"
    fi

    if dmsetup create "$mapping_name" --table "$mapping_table"; then
        log "created /dev/mapper/$mapping_name"
        return 0
    fi

    # A second settled-hook invocation may have won the create race.
    if dmsetup info "$mapping_name" >/dev/null 2>&1 \
        && mapping_matches "$mapping_name" "$mapping_sectors" "$mapping_start"; then
        return 0
    fi
    fail "could not create /dev/mapper/$mapping_name"
}

ensure_mapping "$boot_name" "$boot_sectors" "$boot_start" "$boot_table"
ensure_mapping "$pv_name" "$pv_sectors" "$pv_start" "$pv_table"
udevadm settle || fail "udevadm did not settle after creating mappings"

exit 0
