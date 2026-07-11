#!/bin/sh

set -eu

LC_ALL=C
export LC_ALL

TEST_DIR=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH='' cd -P -- "$TEST_DIR/.." && pwd)
# shellcheck disable=SC1091
. "$ROOT/lib/patchset.sh"

fail()
{
	printf 'not ok - %s\n' "$*" >&2
	exit 1
}

for command_name in git mktemp rm; do
	command -v "$command_name" >/dev/null 2>&1 ||
		fail "required command is unavailable: $command_name"
done

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/frankensargo-patchset.XXXXXX")
trap 'rm -rf "$tmpdir"' 0 1 2 15

repo=$tmpdir/source
patches=$tmpdir/patches
mkdir -p "$repo" "$patches"
git -C "$repo" init -q
git -C "$repo" config user.name 'frankensargo test'
git -C "$repo" config user.email 'test@invalid'
printf 'base\n' >"$repo/one.txt"
printf 'keep\n' >"$repo/two.txt"
git -C "$repo" add one.txt two.txt
git -C "$repo" commit -qm base
revision=$(git -C "$repo" rev-parse HEAD)

cat >"$patches/0001-change-one.patch" <<'EOF'
diff --git a/one.txt b/one.txt
index df967b9..6bd9ad4 100644
--- a/one.txt
+++ b/one.txt
@@ -1 +1 @@
-base
+patched
EOF
cat >"$patches/0002-add-three.patch" <<'EOF'
diff --git a/three.txt b/three.txt
new file mode 100644
index 0000000..2bdf67a
--- /dev/null
+++ b/three.txt
@@ -0,0 +1 @@
+new
EOF

patchset_verify_or_apply "$repo" "$revision" "$patches" ||
	fail 'clean base did not accept the patch stack'
[ "$(cat "$repo/one.txt")" = patched ] || fail 'first patch was not applied'
[ "$(cat "$repo/three.txt")" = new ] || fail 'new file patch was not applied'

first_tree=$(patchset_worktree_tree "$repo" "$revision")
patchset_verify_or_apply "$repo" "$revision" "$patches" ||
	fail 'exact patch stack was not idempotently accepted'
second_tree=$(patchset_worktree_tree "$repo" "$revision")
[ "$first_tree" = "$second_tree" ] || fail 'idempotent verification changed the tree'

printf 'unrelated\n' >"$repo/two.txt"
if patchset_verify_or_apply "$repo" "$revision" "$patches" >/dev/null 2>&1; then
	fail 'unrelated tracked change was accepted'
fi
printf 'keep\n' >"$repo/two.txt"

printf 'surprise\n' >"$repo/untracked.txt"
if patchset_verify_or_apply "$repo" "$revision" "$patches" >/dev/null 2>&1; then
	fail 'unrelated untracked file was accepted'
fi
rm -f "$repo/untracked.txt"

empty_patches=$tmpdir/empty-patches
mkdir "$empty_patches"
if patchset_verify_or_apply "$repo" "$revision" "$empty_patches" >/dev/null 2>&1; then
	fail 'an empty patch directory was accepted'
fi

printf 'ok - exact Git patch-stack verification\n'
