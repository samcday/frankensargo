#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

"$ROOT/bin/source-status" --help >/dev/null
"$ROOT/bin/build-pocketboot" --help >/dev/null

grep -Eq '^POCKETBOOT_REV=[0-9a-f]{40}$' "$ROOT/config/sources.lock"
grep -Eq '^SDM670_KERNEL_REV=[0-9a-f]{40}$' "$ROOT/config/sources.lock"
grep -Eq '^LK2ND2ND_REV=[0-9a-f]{40}$' "$ROOT/config/sources.lock"
grep -Fq 'pocketboot.log=debug pocketboot.acm' \
    "$ROOT/patches/pocketboot/0001-sargo-lab-console.patch"

echo "source tool tests: PASS"
