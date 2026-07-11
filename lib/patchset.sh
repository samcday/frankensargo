#!/bin/sh

# Deterministic Git patch-stack preparation. These helpers compare complete
# Git trees through temporary indexes, so an expected one-line edit cannot
# accidentally bless unrelated tracked or untracked source changes.

patchset_error()
{
	printf 'patchset: %s\n' "$*" >&2
}

patchset_new_index()
{
	patchset_index=$(mktemp "${TMPDIR:-/tmp}/frankensargo-index.XXXXXX") ||
		return 1
	rm -f "$patchset_index" || return 1
	printf '%s\n' "$patchset_index"
}

patchset_have_patches()
{
	patchset_dir=$1
	set -- "$patchset_dir"/*.patch
	[ -f "$1" ]
}

patchset_expected_tree()
{
	patchset_repo=$1
	patchset_revision=$2
	patchset_dir=$3

	if ! patchset_have_patches "$patchset_dir"; then
		patchset_error "no patch files in $patchset_dir"
		return 1
	fi
	patchset_index=$(patchset_new_index) || return 1
	patchset_status=0

	GIT_INDEX_FILE=$patchset_index git -C "$patchset_repo" \
		read-tree "$patchset_revision" || patchset_status=$?
	if [ "$patchset_status" -eq 0 ]; then
		for patchset_patch in "$patchset_dir"/*.patch; do
			GIT_INDEX_FILE=$patchset_index git -C "$patchset_repo" \
				apply --cached --whitespace=nowarn "$patchset_patch" || {
				patchset_status=$?
				break
			}
		done
	fi
	if [ "$patchset_status" -eq 0 ]; then
		GIT_INDEX_FILE=$patchset_index git -C "$patchset_repo" write-tree ||
			patchset_status=$?
	fi
	rm -f "$patchset_index"
	return "$patchset_status"
}

patchset_worktree_tree()
{
	patchset_repo=$1
	patchset_revision=$2
	patchset_index=$(patchset_new_index) || return 1
	patchset_status=0

	GIT_INDEX_FILE=$patchset_index git -C "$patchset_repo" \
		read-tree "$patchset_revision" || patchset_status=$?
	if [ "$patchset_status" -eq 0 ]; then
		GIT_INDEX_FILE=$patchset_index git -C "$patchset_repo" \
			add -A -- . || patchset_status=$?
	fi
	if [ "$patchset_status" -eq 0 ]; then
		GIT_INDEX_FILE=$patchset_index git -C "$patchset_repo" write-tree ||
			patchset_status=$?
	fi
	rm -f "$patchset_index"
	return "$patchset_status"
}

patchset_apply()
{
	patchset_repo=$1
	patchset_dir=$2

	if ! patchset_have_patches "$patchset_dir"; then
		patchset_error "no patch files in $patchset_dir"
		return 1
	fi
	for patchset_patch in "$patchset_dir"/*.patch; do
		git -C "$patchset_repo" apply --whitespace=nowarn "$patchset_patch" ||
			return 1
	done
}

# Leave an exact already-patched worktree alone, apply the stack to an exact
# base tree, and reject every other state.
patchset_verify_or_apply()
{
	patchset_repo=$1
	patchset_revision=$2
	patchset_dir=$3

	patchset_base=$(git -C "$patchset_repo" rev-parse "$patchset_revision^{tree}") ||
		return 1
	patchset_expected=$(patchset_expected_tree \
		"$patchset_repo" "$patchset_revision" "$patchset_dir") || return 1
	patchset_actual=$(patchset_worktree_tree \
		"$patchset_repo" "$patchset_revision") || return 1

	if [ "$patchset_actual" = "$patchset_expected" ]; then
		return 0
	fi
	if [ "$patchset_actual" != "$patchset_base" ]; then
		patchset_error "worktree does not match the base or expected patch stack: $patchset_repo"
		return 1
	fi

	patchset_apply "$patchset_repo" "$patchset_dir" || return 1
	patchset_actual=$(patchset_worktree_tree \
		"$patchset_repo" "$patchset_revision") || return 1
	if [ "$patchset_actual" != "$patchset_expected" ]; then
		patchset_error 'worktree differs after applying the expected patch stack'
		return 1
	fi
}
