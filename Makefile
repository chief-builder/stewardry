# Offline checks. `make check` is the one command from a clean clone.
# Prerequisites: GNU Make, uv (https://docs.astral.sh/uv/), git.
# uv installs Python 3.14 itself if it is missing.

UV ?= uv
RUN = $(UV) run --frozen

.PHONY: help install lint format typecheck test docs secrets workflows actionlint check build test-live-model pinned-links audit lock

help:  ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/'

install:  ## Create .venv from the lock (hash-checked)
	$(UV) sync --frozen

lint: install  ## Lint and check formatting
	$(RUN) ruff check .
	$(RUN) ruff format --check .

format: install  ## Apply the formatter and safe lint fixes
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

typecheck: install  ## Type-check src and tools (mypy strict)
	$(RUN) mypy

test: install  ## Offline tests with coverage (network blocked)
	$(RUN) pytest

docs: install  ## Relative links and anchors in Markdown resolve
	$(RUN) python tools/check_docs.py

secrets: install  ## No secret-shaped strings in tracked files
	$(RUN) python tools/check_secrets.py

ACTIONLINT_IMAGE = rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667

workflows: install  ## Audit GitHub Actions workflows (zizmor; actionlint runs in CI)
	$(RUN) zizmor --offline --persona=pedantic .github/workflows

actionlint:  ## Lint workflow syntax with the pinned actionlint image (needs Docker)
	docker run --rm --network=none -v "$(CURDIR):/repo:ro" -w /repo $(ACTIONLINT_IMAGE) -color

check: lint typecheck test docs secrets workflows  ## Everything CI checks offline

build: install  ## Build the wheel and sdist, then check the wheel installs and runs
	rm -rf dist
	$(UV) build
	$(UV) run --isolated --no-project --with dist/*.whl stewardry --version

test-live-model: install  ## Live-model smoke test: one real, tiny API call (needs a key; costs money)
	STEWARDRY_LIVE_MODEL=1 $(RUN) pytest -m live_model --no-cov tests/unit/test_model_live.py

pinned-links:  ## Check GitHub links pinned to a commit SHA via the API (needs network)
	python3 tools/check_pinned_links.py

audit: install  ## pip-audit over the locked dependencies (needs network)
	$(UV) export --frozen --format requirements-txt --no-emit-project --output-file .audit-requirements.txt
	$(RUN) pip-audit --require-hashes --disable-pip -r .audit-requirements.txt
	rm -f .audit-requirements.txt

lock:  ## Move the 14-day cooldown date forward and re-lock
	$(UV) run --no-project python -c "import datetime as d, pathlib, re; p = pathlib.Path('pyproject.toml'); c = (d.datetime.now(d.UTC) - d.timedelta(days=14)).strftime('%Y-%m-%dT00:00:00Z'); p.write_text(re.sub(r'exclude-newer = \"[^\"]+\"', f'exclude-newer = \"{c}\"', p.read_text()))"
	$(UV) lock
