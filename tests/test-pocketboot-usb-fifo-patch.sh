#!/bin/sh

set -eu

LC_ALL=C
export LC_ALL

TEST_DIR=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH='' cd -P -- "$TEST_DIR/.." && pwd)
PATCH_DIR=$ROOT/patches/pocketboot
PATCH=$PATCH_DIR/0012-sdm670-dwc3-tx-fifo-resize.patch

fail()
{
	printf 'not ok - %s\n' "$*" >&2
	exit 1
}

previous=
found=0
for candidate in "$PATCH_DIR"/*.patch; do
	if [ "$candidate" = "$PATCH" ]; then
		[ "${previous##*/}" = 0011-safe-gadget-teardown-recovery.patch ] ||
			fail 'SDM670 TX FIFO patch does not immediately follow patch 0011'
		found=1
		break
	fi
	previous=$candidate
done
[ "$found" -eq 1 ] || fail 'SDM670 TX FIFO patch is absent from the patch stack'

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/frankensargo-usb-fifo-patch.XXXXXX")
trap 'rm -rf "$tmpdir"' 0 1 2 15
repo=$tmpdir/source
expected=$tmpdir/expected.dtso
mkdir -p "$repo"
git -C "$repo" init -q

git -C "$repo" apply --check --whitespace=error-all "$PATCH" ||
	fail 'TX FIFO patch does not apply cleanly'
git -C "$repo" apply "$PATCH" || fail 'TX FIFO patch application failed'

mkdir -p "$(dirname -- "$expected")"
cat >"$expected" <<'EOF'
/dts-v1/;
/plugin/;

&{/soc@0/usb@a6f8800/usb@a600000} {
	tx-fifo-resize;
};
EOF

overlay=$repo/configs/dt-overlays/qcom/sdm670-google-sargo.dtso
cmp "$expected" "$overlay" || fail 'TX FIFO overlay contents differ'
[ "$(git -C "$repo" status --short --untracked-files=all)" = \
	'?? configs/dt-overlays/qcom/sdm670-google-sargo.dtso' ] ||
	fail 'TX FIFO patch changes an unexpected path'
printf 'ok - PocketBoot SDM670 TX FIFO patch ordering and contents\n'
