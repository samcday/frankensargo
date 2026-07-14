#!/bin/sh

set -eu

LC_ALL=C
export LC_ALL

TEST_DIR=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH='' cd -P -- "$TEST_DIR/.." && pwd)
PATCH_DIR=$ROOT/patches/pocketboot
PATCH=$PATCH_DIR/0013-adb-shell-v2-status.patch

fail()
{
	printf 'not ok - %s\n' "$*" >&2
	exit 1
}

previous=
found=0
for candidate in "$PATCH_DIR"/*.patch; do
	if [ "$candidate" = "$PATCH" ]; then
		[ "${previous##*/}" = 0012-sdm670-dwc3-tx-fifo-resize.patch ] ||
			fail 'ADB shell-v2 patch does not immediately follow patch 0012'
		found=1
		break
	fi
	previous=$candidate
done
[ "$found" -eq 1 ] || fail 'ADB shell-v2 patch is absent from the patch stack'

paths=$(git apply --numstat "$PATCH" | sed 's/^[0-9-]*[[:space:]][0-9-]*[[:space:]]//')
[ "$paths" = src/adb.rs ] || fail 'ADB shell-v2 patch changes an unexpected path'

for contract in \
	'features=shell_v2' \
	'SHELL_V2_STDOUT' \
	'SHELL_V2_STDERR' \
	'SHELL_V2_EXIT' \
	'parse_shell_v2_input' \
	'output.wait_for_write_credit()' \
	'advertises_and_recognizes_raw_shell_v2_service' \
	'shell_v2_input_parser_handles_fragmentation_and_close'; do
	grep -F "$contract" "$PATCH" >/dev/null ||
		fail "ADB shell-v2 patch lacks contract: $contract"
done

printf 'ok - PocketBoot raw ADB shell-v2 patch ordering and contracts\n'
