#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

python3 "$ROOT/tests/test-pbread1-backup.py"
"$ROOT/bin/backup-pbread1" --help >/dev/null

echo 'PBREAD1 backup tests: PASS'
