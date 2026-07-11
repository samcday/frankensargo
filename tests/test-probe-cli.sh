#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
PROBE=$ROOT/bin/probe-fastboot
"$PROBE" --help >/dev/null

if "$PROBE" >/dev/null 2>&1; then
    echo "probe CLI test: missing serial unexpectedly succeeded" >&2
    exit 1
fi

if "$PROBE" --serial=-surprise >/dev/null 2>&1; then
    echo "probe CLI test: option-like serial unexpectedly succeeded" >&2
    exit 1
fi

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/frankensargo-probe.XXXXXX")
trap 'rm -rf "$tmpdir"' 0 1 2 15

cat >"$tmpdir/fastboot" <<'EOF'
#!/bin/sh
set -eu
printf '%s\n' "$*" >>"$FASTBOOT_TEST_LOG"
[ "$#" -eq 4 ] || exit 91
[ "$1" = -s ] || exit 92
[ "$2" = SPARE-SARGO ] || exit 93
[ "$3" = getvar ] || exit 94
case $4 in
    product) printf '(bootloader) product: sargo\n' >&2 ;;
    serialno) printf '(bootloader) serialno: %s\n' "${FASTBOOT_TEST_REPORTED_SERIAL:-SPARE-SARGO}" >&2 ;;
    current-slot) printf '(bootloader) current-slot: a\n' >&2 ;;
    unlocked) printf '(bootloader) unlocked: yes\n' >&2 ;;
    secure) printf '(bootloader) secure: yes\n' >&2 ;;
    max-download-size) printf '(bootloader) max-download-size: 0x10000000\n' >&2 ;;
    battery-voltage) printf '(bootloader) battery-voltage: 4000mV\n' >&2 ;;
    *) exit 95 ;;
esac
EOF
chmod +x "$tmpdir/fastboot"

FASTBOOT_TEST_LOG=$tmpdir/calls
export FASTBOOT_TEST_LOG
PATH=$tmpdir:$PATH "$PROBE" --serial SPARE-SARGO >"$tmpdir/output"

[ "$(wc -l <"$FASTBOOT_TEST_LOG")" -eq 7 ] || {
    echo "probe CLI test: unexpected fastboot call count" >&2
    exit 1
}
if grep -Ev '^-s SPARE-SARGO getvar (product|serialno|current-slot|unlocked|secure|max-download-size|battery-voltage)$' \
    "$FASTBOOT_TEST_LOG" >/dev/null; then
    echo "probe CLI test: unscoped or mutating fastboot command observed" >&2
    cat "$FASTBOOT_TEST_LOG" >&2
    exit 1
fi
if grep -Eq '(^| )devices($| )|(^| )(boot|flash|erase|reboot|continue|set_active)(:| |$)' \
    "$FASTBOOT_TEST_LOG"; then
    echo "probe CLI test: forbidden fastboot verb observed" >&2
    exit 1
fi
grep -q '^\[product\]$' "$tmpdir/output" || {
    echo "probe CLI test: product output is missing" >&2
    exit 1
}

: >"$FASTBOOT_TEST_LOG"
if FASTBOOT_TEST_REPORTED_SERIAL=DIFFERENT-SARGO \
    PATH=$tmpdir:$PATH "$PROBE" --serial SPARE-SARGO >"$tmpdir/mismatch-output" 2>"$tmpdir/mismatch-error"; then
    echo "probe CLI test: mismatched bootloader serial unexpectedly succeeded" >&2
    exit 1
fi
[ "$(wc -l <"$FASTBOOT_TEST_LOG")" -eq 2 ] || {
    echo "probe CLI test: serial mismatch did not stop immediately" >&2
    cat "$FASTBOOT_TEST_LOG" >&2
    exit 1
}
grep -Fq 'bootloader serialno does not match requested serial' "$tmpdir/mismatch-error" || {
    echo "probe CLI test: serial mismatch diagnostic is missing" >&2
    cat "$tmpdir/mismatch-error" >&2
    exit 1
}
if grep -Ev '^-s SPARE-SARGO getvar (product|serialno)$' "$FASTBOOT_TEST_LOG" >/dev/null; then
    echo "probe CLI test: serial mismatch path issued an unexpected fastboot command" >&2
    cat "$FASTBOOT_TEST_LOG" >&2
    exit 1
fi

echo "probe CLI tests: PASS"
