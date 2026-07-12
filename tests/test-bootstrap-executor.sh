#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

"$ROOT/bin/execute-bootstrap" --help >/dev/null
if "$ROOT/bin/execute-bootstrap" \
	--plan "$ROOT/examples/bootstrap-plan-v1.example.json" \
	--serial SYNTHETIC-SARGO \
	--partuuid 11111111-2222-4333-8444-555555555555 \
	--confirm BOOTSTRAP-01234567-dummy \
	--recovery-attestation ABL-AND-SYSRQ-RECOVERY-PROVEN \
	--pbread-run /definitely/not/used-pbread \
	--state-dir /definitely/not/used >/dev/null 2>&1; then
	echo 'executor contacted or accepted inputs without --execute' >&2
	exit 1
fi

python3 "$ROOT/tests/test-bootstrap-executor.py"
