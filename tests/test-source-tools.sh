#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

"$ROOT/bin/source-status" --help >/dev/null
help=$("$ROOT/bin/build-pocketboot" --help)
printf '%s\n' "$help" | grep -Fq -- '--no-acm'

grep -Eq '^POCKETBOOT_REV=[0-9a-f]{40}$' "$ROOT/config/sources.lock"
grep -Eq '^SDM670_KERNEL_REV=[0-9a-f]{40}$' "$ROOT/config/sources.lock"
grep -Eq '^LK2ND2ND_REV=[0-9a-f]{40}$' "$ROOT/config/sources.lock"
grep -Fq 'pocketboot.log=debug pocketboot.acm' \
    "$ROOT/patches/pocketboot/0001-sargo-lab-console.patch"
grep -Fq 'pocketboot-sargo-lab-noacm.img' "$ROOT/bin/build-pocketboot"
grep -Fq '.frankensargo-build.lock' "$ROOT/bin/build-pocketboot"
grep -Fq '.frankensargo-build.lock' "$ROOT/bin/build-pocketboot-bound"
grep -Fq 'android-v2-header-postprocess' "$ROOT/bin/build-pocketboot"
grep -Fq 'profile_parent_sha256=' "$ROOT/bin/build-pocketboot"
grep -Fq 'profile_changed_spans=' "$ROOT/bin/build-pocketboot"
grep -Fq 'pocketboot_effective_tree=' "$ROOT/bin/build-pocketboot"
grep -Fq 'pocketboot_bundle.py' "$ROOT/bin/build-pocketboot"
grep -Fq '.bundle.json' "$ROOT/bin/build-pocketboot"

echo "source tool tests: PASS"
