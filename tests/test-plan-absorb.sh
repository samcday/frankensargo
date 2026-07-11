#!/bin/sh

set -eu

LC_ALL=C
export LC_ALL

TEST_DIR=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH='' cd -P -- "$TEST_DIR/.." && pwd)
PLANNER=$ROOT/target/plan-absorb
SCHEMA=$ROOT/schema/grey-goo-manifest-v1.schema.json
EXAMPLE=$ROOT/examples/grey-goo-manifest-v1.example.json
PARTITION_ID='gpt:11111111-2222-4333-8444-555555555555/66666666-7777-4888-9999-aaaaaaaaaaaa'
OPERATION_UUID='01234567-89ab-4cde-8f01-23456789abcd'

fail()
{
	printf 'not ok - %s\n' "$*" >&2
	exit 1
}

assert_eq()
{
	assert_name=$1
	assert_expected=$2
	assert_actual=$3

	[ "$assert_expected" = "$assert_actual" ] || {
		printf 'not ok - %s\nexpected: %s\nactual:   %s\n' \
			"$assert_name" "$assert_expected" "$assert_actual" >&2
		exit 1
	}
}

run_plan()
{
	"$PLANNER" \
		--manifest "$1" \
		--partition-id "$PARTITION_ID" \
		--operation-uuid "$OPERATION_UUID"
}

expect_rejected()
{
	reject_name=$1
	reject_filter=$2
	reject_manifest=$tmpdir/rejected.json

	jq "$reject_filter" "$EXAMPLE" >"$reject_manifest"
	jq empty "$reject_manifest"
	if run_plan "$reject_manifest" >"$tmpdir/rejected.out" 2>&1; then
		fail "$reject_name unexpectedly produced an authorization plan"
	fi
}

for command_name in cmp cut grep jq jsonschema mktemp rm sha256sum; do
	command -v "$command_name" >/dev/null 2>&1 ||
		fail "required command is unavailable: $command_name"
done

[ -x "$PLANNER" ] || fail "planner is not executable: $PLANNER"

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/frankensargo-plan-absorb.XXXXXX")
trap 'rm -rf "$tmpdir"' 0 1 2 15

jq empty "$SCHEMA"
jq empty "$EXAMPLE"
jsonschema "$SCHEMA" -i "$EXAMPLE" >/dev/null 2>&1 ||
	fail 'synthetic example does not satisfy the v1 schema'

manifest_before=$(sha256sum "$EXAMPLE")
manifest_before=${manifest_before%% *}
run_plan "$EXAMPLE" >"$tmpdir/plan-a.json"
manifest_after=$(sha256sum "$EXAMPLE")
manifest_after=${manifest_after%% *}
assert_eq 'planner leaves manifest unchanged' "$manifest_before" "$manifest_after"

jq empty "$tmpdir/plan-a.json"
run_plan "$EXAMPLE" >"$tmpdir/plan-b.json"
cmp -s "$tmpdir/plan-a.json" "$tmpdir/plan-b.json" ||
	fail 'authorization plan is not deterministic'

assert_eq 'authorization schema' \
	'org.pocketboot.greygoo.absorb-authorization/1' \
	"$(jq -r '.schema' "$tmpdir/plan-a.json")"
assert_eq 'operation UUID' "$OPERATION_UUID" \
	"$(jq -r '.operation_uuid' "$tmpdir/plan-a.json")"
assert_eq 'capsule VG binding' \
	'VG0000-1111-2222-3333-4444-5555-666666' \
	"$(jq -r '.capsule_binding.vg_uuid' "$tmpdir/plan-a.json")"
assert_eq 'exact current PV allowlist' '1' \
	"$(jq -r '.lvm.allowed_pvs | length' "$tmpdir/plan-a.json")"
assert_eq 'exact partition ID' "$PARTITION_ID" \
	"$(jq -r '.partition.id' "$tmpdir/plan-a.json")"
assert_eq 'planned PV UUID' \
	'DONOR0-1111-2222-3333-4444-5555-666666' \
	"$(jq -r '.partition.planned_pv_uuid' "$tmpdir/plan-a.json")"
assert_eq 'positive net capacity' '2415919104' \
	"$(jq -r '.capacity.conservative_net_bytes' "$tmpdir/plan-a.json")"
assert_eq 'verified copy included' \
	'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee' \
	"$(jq -r '.archive.verified_off_device_copy_ids[0]' "$tmpdir/plan-a.json")"
assert_eq 'LP extent gate included' 'true' \
	"$(jq -r '.lp_extent_gate.required and
		.lp_extent_gate.status == "satisfied" and
		.lp_extent_gate.all_touching_extents_archived and
		.lp_extent_gate.archive_mappings_verified' "$tmpdir/plan-a.json")"
assert_eq 'firmware gate included' 'true' \
	"$(jq -r '.firmware_gate.required and
		.firmware_gate.load_test_passed and
		.firmware_gate.off_device_verified' "$tmpdir/plan-a.json")"

manifest_digest=$(jq -cS . "$EXAMPLE" | sha256sum)
manifest_digest=sha256:${manifest_digest%% *}
assert_eq 'canonical manifest digest' "$manifest_digest" \
	"$(jq -r '.manifest.canonical_sha256' "$tmpdir/plan-a.json")"

authorization_core=$(jq -cS \
	'del(.authorization_sha256, .confirmation)' "$tmpdir/plan-a.json")
authorization_digest=$(printf '%s\n' "$authorization_core" | sha256sum)
authorization_digest=sha256:${authorization_digest%% *}
assert_eq 'authorization core digest' "$authorization_digest" \
	"$(jq -r '.authorization_sha256' "$tmpdir/plan-a.json")"
authorization_hex=${authorization_digest#sha256:}
authorization_prefix=$(printf '%s' "$authorization_hex" | cut -c 1-12)
expected_token="ABSORB-01234567-$authorization_prefix"
assert_eq 'explicit confirmation token' "$expected_token" \
	"$(jq -r '.confirmation.token' "$tmpdir/plan-a.json")"

if grep -Eiq 'pvcreate|vgextend|wipefs|blkdiscard|sfdisk|dmsetup' \
	"$tmpdir/plan-a.json"; then
	fail 'read-only planner emitted a write-capable command name'
fi

if "$PLANNER" --manifest "$EXAMPLE" \
	--partition-id 'gpt:missing/missing' \
	--operation-uuid "$OPERATION_UUID" >/dev/null 2>&1; then
	fail 'unknown partition ID was accepted'
fi

if "$PLANNER" --manifest "$EXAMPLE" \
	--partition-id "$PARTITION_ID" \
	--operation-uuid 'not-a-uuid' >/dev/null 2>&1; then
	fail 'malformed operation UUID was accepted'
fi

expect_rejected 'duplicate partition ID' \
	'.partitions += [.partitions[0]]'
expect_rejected 'non-ready partition state' \
	'.partitions[0].takeover.state = "archive-verified"'
expect_rejected 'mutable identity' \
	'.partitions[0].identity.immutable = false'
expect_rejected 'mutable geometry' \
	'.partitions[0].geometry.immutable = false'
expect_rejected 'mutable raw hash record' \
	'.partitions[0].raw.immutable = false'
expect_rejected 'mutable planned PV UUID' \
	'.partitions[0].takeover.planned_pv_uuid_immutable = false'
expect_rejected 'identity binding mismatch' \
	'.partitions[0].geometry.start_lba = "1048577"'
expect_rejected 'planned PV binding mismatch' \
	'.partitions[0].takeover.planned_pv_uuid = "CHANGE-1111-2222-3333-4444-5555-666666"'
expect_rejected 'raw geometry mismatch' \
	'.partitions[0].raw.bytes = "6442450432"'
expect_rejected 'archive source mismatch' \
	'.objects[0].source_partition_id = "gpt:somewhere/else"'
expect_rejected 'unverified off-device copy' \
	'.objects[0].off_device_copies[0].verified = false'
expect_rejected 'off-device hash mismatch' \
	'.objects[0].off_device_copies[0].sha256 =
	 "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"'
expect_rejected 'pending required firmware gate' \
	'.firmware_gate.status = "pending"'
expect_rejected 'unverified off-device firmware bundle' \
	'.firmware_gate.off_device_verified = false'
expect_rejected 'failed firmware load test' \
	'.firmware_gate.load_test.passed = false'
expect_rejected 'pending Android LP extent gate' \
	'.partitions[0].lp_extent_gate.status = "pending"'
expect_rejected 'unarchived touching LP extent' \
	'.partitions[0].lp_extent_gate.all_touching_extents_archived = false'
expect_rejected 'unverified archive LP mappings' \
	'.partitions[0].lp_extent_gate.archive_mappings_verified = false'
expect_rejected 'wrong archive kind for LP container' \
	'.objects[0].kind = "raw-partition"'
expect_rejected 'active operation' \
	'.active_operation = {
	  operation_uuid: "fedcba98-7654-4321-8fed-cba987654321",
	  partition_id: .partitions[0].id,
	  state: "absorb-intent"
	}'
expect_rejected 'capsule VG mismatch' \
	'.capsule_binding.vg_uuid = "OTHER0-1111-2222-3333-4444-5555-666666"'
expect_rejected 'candidate planned PV UUID already allowed' '
	.lvm.allowed_pvs += [{
	  partition_id: .partitions[0].id,
	  partuuid: .partitions[0].identity.partuuid,
	  pv_uuid: .partitions[0].takeover.planned_pv_uuid
	}]
'
expect_rejected 'zero conservative net capacity' '
	.partitions[0].capacity.retained_allocated_bytes =
	  .partitions[0].capacity.donor_bytes |
	.partitions[0].capacity.metadata_growth_bytes = "0" |
	.partitions[0].capacity.reserve_bytes = "0" |
	.partitions[0].capacity.conservative_net_bytes = "0"
'
expect_rejected 'inconsistent conservative net capacity' \
	'.partitions[0].capacity.conservative_net_bytes = "1"'

no_firmware_manifest=$tmpdir/no-firmware.json
jq '
	.partitions[0].firmware_gate_required = false |
	.firmware_gate.status = "pending" |
	.firmware_gate.off_device_verified = false |
	.firmware_gate.load_test.passed = false
	' "$EXAMPLE" >"$no_firmware_manifest"
jq empty "$no_firmware_manifest"
run_plan "$no_firmware_manifest" >"$tmpdir/no-firmware-plan.json"
jq -e '.firmware_gate == {"required": false}' \
	"$tmpdir/no-firmware-plan.json" >/dev/null ||
	fail 'non-required firmware gate was not handled explicitly'

no_lp_manifest=$tmpdir/no-lp.json
jq '
	.partitions[0].lp_extent_gate = {
	  required: false,
	  status: "not-required",
	  metadata_sha256: null,
	  extent_graph_sha256: null,
	  all_touching_extents_archived: false,
	  archive_mappings_verified: false
	}
	' "$EXAMPLE" >"$no_lp_manifest"
jq empty "$no_lp_manifest"
run_plan "$no_lp_manifest" >"$tmpdir/no-lp-plan.json"
jq -e '.lp_extent_gate.required == false and
	.lp_extent_gate.status == "not-required"' \
	"$tmpdir/no-lp-plan.json" >/dev/null ||
	fail 'non-required Android LP extent gate was not handled explicitly'

printf 'ok - grey-goo absorb authorization planner\n'
