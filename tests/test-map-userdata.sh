#!/bin/sh

set -eu

repo_root=$(CDPATH='' cd "$(dirname "$0")/.." && pwd)
mapper=$repo_root/initramfs/dracut/90frankensargo/map-userdata.sh
tmpdir=${TMPDIR:-/tmp}/frankensargo-map-test.$$

cleanup() {
    rm -rf "$tmpdir"
}
trap cleanup EXIT HUP INT TERM

fail() {
    printf 'not ok - %s\n' "$*" >&2
    exit 1
}

pass() {
    printf 'ok - %s\n' "$1"
}

make_valid_image() {
    image=$1
    dd if=/dev/zero of="$image" bs=512 count=32 2>/dev/null

    # Entry 1: active Linux, LBA 1, 8 sectors.
    printf '\200\000\000\000\203\000\000\000\001\000\000\000\010\000\000\000' \
        | dd of="$image" bs=1 seek=446 conv=notrunc 2>/dev/null

    # Entry 2: inactive Linux LVM, LBA 9, 16 sectors.
    printf '\000\000\000\000\216\000\000\000\011\000\000\000\020\000\000\000' \
        | dd of="$image" bs=1 seek=462 conv=notrunc 2>/dev/null

    printf '\125\252' \
        | dd of="$image" bs=1 seek=510 conv=notrunc 2>/dev/null
}

run_print() {
    FRANKEN_CMDLINE="franken.userdata=$1 franken.boot_start=${2:-1} franken.boot_sectors=${3:-8} franken.pv_start=${4:-9} franken.pv_sectors=${5:-16}" \
        sh "$mapper" --print-tables
}

expect_failure() {
    failure_name=$1
    shift
    if "$@" > "$tmpdir/failure.out" 2>&1; then
        fail "$failure_name was accepted"
    fi
    pass "$failure_name rejected"
}

mkdir -p "$tmpdir"
valid=$tmpdir/userdata.img
make_valid_image "$valid"

expected="franken-boot: 0 8 linear $valid 1
franken-userdata-pv: 0 16 linear $valid 9"
actual=$(run_print "$valid")
[ "$actual" = "$expected" ] || fail "valid MBR produced unexpected tables"
[ "$(run_print "$valid")" = "$actual" ] || fail "print mode is not deterministic"
pass 'valid nested MBR produces deterministic dm-linear tables'

config=$tmpdir/frankensargo-map.conf
{
    printf 'FRANKEN_USERDATA=%s\n' "$valid"
    printf 'FRANKEN_BOOT_START=1\n'
    printf 'FRANKEN_BOOT_SECTORS=8\n'
    printf 'FRANKEN_PV_START=9\n'
    printf 'FRANKEN_PV_SECTORS=16\n'
} > "$config"
config_actual=$(FRANKEN_CMDLINE='' sh "$mapper" --print-tables --config "$config")
[ "$config_actual" = "$expected" ] || fail "config fallback produced unexpected tables"
pass '/etc-style environment fallback is parsed'

bad=$tmpdir/bad.img

cp "$valid" "$bad"
printf '\000' | dd of="$bad" bs=1 seek=510 conv=notrunc 2>/dev/null
expect_failure 'bad MBR signature' run_print "$bad"

cp "$valid" "$bad"
printf '\000' | dd of="$bad" bs=1 seek=446 conv=notrunc 2>/dev/null
expect_failure 'inactive boot entry' run_print "$bad"

cp "$valid" "$bad"
printf '\203' | dd of="$bad" bs=1 seek=466 conv=notrunc 2>/dev/null
expect_failure 'wrong PV partition type' run_print "$bad"

expect_failure 'unexpected boot geometry' run_print "$valid" 2 8 9 16

cp "$valid" "$bad"
printf '\036\000\000\000' \
    | dd of="$bad" bs=1 seek=474 conv=notrunc 2>/dev/null
expect_failure 'partition beyond userdata size' run_print "$bad" 1 8 9 30

cp "$valid" "$bad"
printf '\010\000\000\000' \
    | dd of="$bad" bs=1 seek=470 conv=notrunc 2>/dev/null
expect_failure 'overlapping nested partitions' run_print "$bad" 1 8 8 16

pass 'all mapper parser tests completed'
