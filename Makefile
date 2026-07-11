SHELL := /bin/sh

SHELL_SOURCES := $(shell rg -l '^\#! */bin/(ba)?sh' bin lib target tests initramfs 2>/dev/null)
TESTS := $(sort $(wildcard tests/test-*.sh))

.PHONY: check shellcheck test source-status build-pocketboot

check: shellcheck test
	git diff --check

shellcheck:
	shellcheck -s sh $(SHELL_SOURCES)

test:
	@set -e; for test in $(TESTS); do \
		printf '\n==> %s\n' "$$test"; \
		"$$test"; \
	done

source-status:
	bin/source-status

build-pocketboot:
	bin/build-pocketboot
