#!/bin/sh

set -eu

LC_ALL=C
export LC_ALL

TEST_DIR=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH='' cd -P -- "$TEST_DIR/.." && pwd)
PLANNER=$ROOT/target/plan-discovery
SCHEMA=$ROOT/schema/grey-goo-manifest-v1.schema.json
EXAMPLE=$ROOT/examples/grey-goo-manifest-v1.example.json
ANCHOR_PARTUUID='22222222-3333-4444-8555-bbbbbbbbbbbb'
ANCHOR_PV_UUID='PV0000-1111-2222-3333-4444-5555-666666'

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

expect_rejected()
{
	reject_name=$1
	reject_filter=$2
	reject_manifest=$tmpdir/rejected.json

	jq "$reject_filter" "$EXAMPLE" >"$reject_manifest"
	jq empty "$reject_manifest"
	if "$PLANNER" --manifest "$reject_manifest" \
		>"$tmpdir/rejected.out" 2>&1; then
		fail "$reject_name unexpectedly produced a discovery plan"
	fi
}

for command_name in cmp grep jq jsonschema mktemp rm sha256sum; do
	command -v "$command_name" >/dev/null 2>&1 ||
		fail "required command is unavailable: $command_name"
done

[ -x "$PLANNER" ] || fail "planner is not executable: $PLANNER"

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/frankensargo-plan-discovery.XXXXXX")
trap 'rm -rf "$tmpdir"' 0 1 2 15

jsonschema "$SCHEMA" -i "$EXAMPLE" >/dev/null 2>&1 ||
	fail 'synthetic example does not satisfy the v1 schema'

manifest_before=$(sha256sum "$EXAMPLE")
manifest_before=${manifest_before%% *}
"$PLANNER" --manifest "$EXAMPLE" >"$tmpdir/plan-a.json"
manifest_after=$(sha256sum "$EXAMPLE")
manifest_after=${manifest_after%% *}
assert_eq 'planner leaves manifest unchanged' "$manifest_before" "$manifest_after"

"$PLANNER" --manifest "$EXAMPLE" >"$tmpdir/plan-b.json"
cmp -s "$tmpdir/plan-a.json" "$tmpdir/plan-b.json" ||
	fail 'discovery plan is not deterministic'

assert_eq 'discovery schema' \
	'org.pocketboot.greygoo.discovery-plan/1' \
	"$(jq -r '.schema' "$tmpdir/plan-a.json")"
assert_eq 'capsule anchor PARTUUID' "$ANCHOR_PARTUUID" \
	"$(jq -r '.capsule_binding.anchor_partuuid' "$tmpdir/plan-a.json")"
assert_eq 'stage-one exact anchor PV' "$ANCHOR_PV_UUID" \
	"$(jq -r '.stage1_anchor_only.devices[0].pv_uuid' "$tmpdir/plan-a.json")"
assert_eq 'stage-one device count' '1' \
	"$(jq -r '.stage1_anchor_only.devices | length' "$tmpdir/plan-a.json")"
assert_eq 'stage-one metadata-only role' 'pocketboot.meta.v1' \
	"$(jq -r '.stage1_anchor_only.eligible_lv_tags | join(",")' \
		"$tmpdir/plan-a.json")"
assert_eq 'stage-two device count' '1' \
	"$(jq -r '.stage2_manifest_allowlist.devices | length' \
		"$tmpdir/plan-a.json")"
assert_eq 'guest LVM recursion forbidden' 'false' \
	"$(jq -r '.constraints.recursive_guest_lvm_scan' \
		"$tmpdir/plan-a.json")"

manifest_digest=$(jq -cS . "$EXAMPLE" | sha256sum)
manifest_digest=sha256:${manifest_digest%% *}
assert_eq 'canonical manifest digest' "$manifest_digest" \
	"$(jq -r '.manifest.canonical_sha256' "$tmpdir/plan-a.json")"

if grep -Eiq 'pvcreate|vgextend|wipefs|blkdiscard|sfdisk|dmsetup' \
	"$tmpdir/plan-a.json"; then
	fail 'read-only discovery planner emitted a write-capable command name'
fi

expect_rejected 'capsule VG mismatch' \
	'.capsule_binding.vg_uuid = "OTHER0-1111-2222-3333-4444-5555-666666"'
expect_rejected 'capsule anchor PARTUUID missing from allowlist' \
	'.capsule_binding.anchor_partuuid =
	 "33333333-4444-4555-8666-cccccccccccc"'
expect_rejected 'anchor PV UUID mismatch' \
	'.lvm.anchor_pv_uuid = "OTHER0-1111-2222-3333-4444-5555-666666"'
expect_rejected 'allowed PV partition identity mismatch' \
	'.lvm.allowed_pvs[0].partition_id =
	 "gpt:11111111-2222-4333-8444-555555555555/33333333-4444-4555-8666-cccccccccccc"'
expect_rejected 'duplicate allowed PV identity' \
	'.lvm.allowed_pvs += [.lvm.allowed_pvs[0]]'
expect_rejected 'duplicate planned PV UUID' '
	.partitions += [(.partitions[0] |
	  .id = "gpt:11111111-2222-4333-8444-555555555555/33333333-4444-4555-8666-cccccccccccc" |
	  .identity.partuuid = "33333333-4444-4555-8666-cccccccccccc")]
'
expect_rejected 'candidate already in allowed PV set' '
	.lvm.allowed_pvs += [{
	  partition_id: .partitions[0].id,
	  partuuid: .partitions[0].identity.partuuid,
	  pv_uuid: .partitions[0].takeover.planned_pv_uuid
	}]
'
expect_rejected 'member missing from allowed PV set' '
	.partitions[0].classification = "member" |
	.partitions[0].takeover.state = "vg-member-fenced"
'

member_manifest=$tmpdir/member.json
jq '
	.partitions[0].classification = "member" |
	.partitions[0].takeover.state = "vg-member-fenced" |
	.lvm.allowed_pvs += [{
	  partition_id: .partitions[0].id,
	  partuuid: .partitions[0].identity.partuuid,
	  pv_uuid: .partitions[0].takeover.planned_pv_uuid
	}]
	' "$EXAMPLE" >"$member_manifest"
jsonschema "$SCHEMA" -i "$member_manifest" >/dev/null 2>&1 ||
	fail 'synthetic member manifest does not satisfy the v1 schema'
"$PLANNER" --manifest "$member_manifest" >"$tmpdir/member-plan.json"
assert_eq 'adding a member does not widen stage one' '1' \
	"$(jq -r '.stage1_anchor_only.devices | length' \
		"$tmpdir/member-plan.json")"
assert_eq 'absorbed member appears in stage two' '2' \
	"$(jq -r '.stage2_manifest_allowlist.devices | length' \
		"$tmpdir/member-plan.json")"

printf 'ok - grey-goo two-stage discovery planner\n'
