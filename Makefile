PYTHON ?= python3

.PHONY: all audit pdf-audit repository privacy epub verify pre-push clean

all: audit epub

audit:
	$(PYTHON) scripts/audit_sources.py
	$(PYTHON) scripts/audit_chinese_variants.py
	$(PYTHON) scripts/audit_math_punctuation.py

pdf-audit:
	$(PYTHON) scripts/audit_pdfs.py

repository:
	$(PYTHON) scripts/audit_repository.py

privacy:
	$(PYTHON) scripts/audit_privacy.py

epub:
	$(PYTHON) scripts/build_epubs.py

verify:
	$(PYTHON) scripts/build_epubs.py --verify-only

pre-push: audit pdf-audit repository privacy verify

clean:
	rm -rf .build dist reports/*.json
