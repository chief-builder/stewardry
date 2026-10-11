# Offline checks. `make check` is the one command from a clean clone.
# Prerequisites: GNU Make, uv (https://docs.astral.sh/uv/), git.
# uv installs Python 3.14 itself if it is missing.

UV ?= uv
RUN = $(UV) run --frozen

.PHONY: help install lint format typecheck test docs secrets workflows actionlint check fixtures build test-live-model record-drafting eval-live eval-replay pinned-links vendored audit lock

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

fixtures: install  ## Rebuild the synthetic questionnaire .xlsx/.docx files from their YAML
	$(RUN) python tools/build_fixtures.py

build: install  ## Build the wheel and sdist, then check the wheel installs and runs
	rm -rf dist
	$(UV) build
	$(UV) run --isolated --no-project --with dist/*.whl stewardry --version

test-live-model: install  ## Live-model smoke test: one real, tiny API call (needs a key; costs money)
	STEWARDRY_LIVE_MODEL=1 $(RUN) pytest -m live_model --no-cov -s tests/unit/test_model_live.py

record-drafting: install  ## Record the drafting smoke scenario against the live model (costs money)
	STEWARDRY_LIVE_MODEL=1 $(RUN) pytest -m live_model --no-cov -s tests/unit/test_drafting_live.py

eval-live: install  ## Live-model baseline on all three questionnaires (records it; costs money)
	STEWARDRY_LIVE_MODEL=1 $(RUN) python -m tools.run_evals --mode live --suite full

eval-replay: install  ## Replay a baseline offline: make eval-replay RECORDINGS=replay/full-<date> [GRADES=...]
	$(RUN) python -m tools.run_evals --mode replay --suite full --recordings $(RECORDINGS) $(if $(GRADES),--grades $(GRADES))

pinned-links:  ## Check GitHub links pinned to a commit SHA via the API (needs network)
	python3 tools/check_pinned_links.py

vendored:  ## Check vendored upstream files against their pinned commit (needs network)
	python3 tools/check_vendored.py

audit: install  ## pip-audit over the locked dependencies (needs network)
	$(UV) export --frozen --format requirements-txt --no-emit-project --output-file .audit-requirements.txt
	$(RUN) pip-audit --require-hashes --disable-pip -r .audit-requirements.txt
	rm -f .audit-requirements.txt

lock:  ## Move the 14-day cooldown date forward and re-lock
	$(UV) run --no-project python -c "import datetime as d, pathlib, re; p = pathlib.Path('pyproject.toml'); c = (d.datetime.now(d.UTC) - d.timedelta(days=14)).strftime('%Y-%m-%dT00:00:00Z'); p.write_text(re.sub(r'exclude-newer = \"[^\"]+\"', f'exclude-newer = \"{c}\"', p.read_text()))"
	$(UV) lock
