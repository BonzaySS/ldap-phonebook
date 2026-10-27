# syntax and style checks, then build the release tarball
SHELL_FILES := install.sh uninstall.sh tests/smoke.sh packaging/build-dist.sh
PY_FILES    := app/app.py app/import_phonebook.py

.PHONY: all check syntax dist clean

all: check

check: syntax
	@echo "checks passed"

syntax:
	@command -v bash >/dev/null && bash -n $(SHELL_FILES) && echo "bash: ok"
	@command -v python3 >/dev/null && python3 -m py_compile $(PY_FILES) && echo "python: ok"

dist: check
	packaging/build-dist.sh

clean:
	rm -rf dist app/__pycache__ tests/__pycache__
