#!/bin/sh

set -eu

repo_root=$(CDPATH='' cd "$(dirname "$0")/.." && pwd)
writer=$repo_root/bin/write-lv-resumable
tmpdir=${TMPDIR:-/tmp}/frankensargo-write-lv-test.$$

cleanup()
{
    rm -rf "$tmpdir"
}
trap cleanup EXIT HUP INT TERM

fail()
{
    printf 'not ok - %s\n' "$*" >&2
    exit 1
}

pass()
{
    printf 'ok - %s\n' "$1"
}

mkdir -p "$tmpdir/bin"
source_image=$tmpdir/source.raw
target_image=$tmpdir/target.raw
prefix_image=$tmpdir/target-prefix.raw
marker=$tmpdir/disconnected-once

truncate -s 9728 "$source_image"
printf 'frankensargo-pocketblue-first' |
    dd of="$source_image" bs=1 seek=0 conv=notrunc 2>/dev/null
printf 'frankensargo-pocketblue-after-drop' |
    dd of="$source_image" bs=1 seek=1024 conv=notrunc 2>/dev/null
printf 'frankensargo-pocketblue-middle' |
    dd of="$source_image" bs=1 seek=4608 conv=notrunc 2>/dev/null
printf 'frankensargo-pocketblue-last' |
    dd of="$source_image" bs=1 seek=9216 conv=notrunc 2>/dev/null
truncate -s 12288 "$target_image"
expected=$(sha256sum "$source_image" | awk '{print $1}')

cat > "$tmpdir/bin/adb" <<'EOF'
#!/bin/sh
set -eu

[ "$1" = -s ] || exit 2
shift 2
case ${1-} in
    get-state)
        printf 'device\n'
        ;;
    shell)
        shift
        command=${1-}
        command=$(printf '%s\n' "$command" |
            sed "s|$FAKE_ADB_DEVICE|$FAKE_ADB_TARGET|g")
        case $command in
            sync)
                exit 0
                ;;
            dd\ of=*)
                if [ ! -e "$FAKE_ADB_MARKER" ]; then
                    : > "$FAKE_ADB_MARKER"
                    dd of="$FAKE_ADB_TARGET" bs=512 count=1 conv=notrunc 2>/dev/null
                    exit 1
                fi
                ;;
        esac
        exec sh -c "$command"
        ;;
    *)
        exit 2
        ;;
esac
EOF
chmod +x "$tmpdir/bin/adb"

run_writer()
{
    PATH=$tmpdir/bin:$PATH \
    FAKE_ADB_DEVICE=/dev/mapper/franken-test \
    FAKE_ADB_TARGET=$target_image \
    FAKE_ADB_MARKER=$marker \
    FRANKENSARGO_WRITE_CHUNK_BYTES=4096 \
    FRANKENSARGO_WRITE_IO_BYTES=1024 \
        "$writer" test-serial "$source_image" /dev/mapper/franken-test "$expected"
}

run_writer > "$tmpdir/first.log"
[ -e "$marker" ] || fail 'fake disconnect was not exercised'
dd if="$target_image" of="$prefix_image" bs=512 count=19 2>/dev/null
cmp "$source_image" "$prefix_image" || fail 'verified target prefix differs from source'
[ "$(stat -c %s "$target_image")" -eq 12288 ] || fail 'writer truncated the larger LV'
grep -F 'chunk 1/3 write attempt 2' "$tmpdir/first.log" >/dev/null ||
    fail 'interrupted first chunk was not retried'
grep -F 'all 3 chunks verified' "$tmpdir/first.log" >/dev/null ||
    fail 'first pass did not publish its verification summary'
pass 'interrupted write resumes and verifies every chunk'

run_writer > "$tmpdir/second.log"
[ "$(grep -c 'already verified' "$tmpdir/second.log")" -eq 3 ] ||
    fail 'second pass did not skip all matching chunks'
if grep -F 'write attempt' "$tmpdir/second.log" >/dev/null; then
    fail 'second pass rewrote an already verified chunk'
fi
pass 'repeat invocation is a read-only verified resume'

if PATH=$tmpdir/bin:$PATH "$writer" test-serial "$source_image" \
    /dev/mmcblk0 "$expected" > "$tmpdir/unsafe.log" 2>&1; then
    fail 'unsafe target was accepted'
fi
pass 'target outside the franken mapper namespace is rejected'
