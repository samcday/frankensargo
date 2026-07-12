#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

python3 "$ROOT/tests/test-plan-bootstrap.py"
"$ROOT/target/plan-bootstrap" --help >/dev/null
jsonschema "$ROOT/schema/bootstrap-plan-v1.schema.json" \
  -i "$ROOT/examples/bootstrap-plan-v1.example.json" >/dev/null 2>&1

echo 'ok - userdata anchor bootstrap planner'
