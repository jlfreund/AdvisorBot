# AdvisorBot developer tasks.
#
# The test suite runs entirely against synthetic fixture portfolios in throwaway
# directories; nothing here reads your own Portfolios/ folder. See Scripts/tests/README.md.
#
# PYTHON defaults to the interpreter that has pytest installed. Override if yours differs:
#     make test PYTHON=python3.12

PYTHON ?= python3.11
PYTEST ?= $(PYTHON) -m pytest
GEN    ?= PYTHONPATH=Scripts $(PYTHON) -m tests.generate
CATALOG ?= PYTHONPATH=Scripts $(PYTHON) -m tests.catalog
BASELINES ?= PYTHONPATH=Scripts $(PYTHON) -m tests.refresh_baselines

.DEFAULT_GOAL := help
.PHONY: help test test-full test-tier test-one test-security fixtures \
        fixtures-check fixtures-root catalog baselines snapshots clean-pyc \
        commit-template

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Fixture portfolios: p0_empty_household p1_index_saver p2_equity_comp_rsu"
	@echo "                      p3_retiree_annuities p4_trust_household p5_adversarial"

test: ## Default set: unit, parsers, pipeline, engine, server, llm, render, scans (~2 min)
	$(PYTEST) -q

test-full: ## Adds parity, the Playwright smoke and slow jobs
	$(PYTEST) -q -m "not network"

test-tier: ## One tier: make test-tier T=server
	@test -n "$(T)" || (echo "usage: make test-tier T=<tier>"; exit 2)
	$(PYTEST) -q Scripts/tests/$(T)

test-one: ## Every test that declares a fixture: make test-one P=p3_retiree_annuities
	@test -n "$(P)" || (echo "usage: make test-one P=<fixture slug>"; exit 2)
	$(PYTEST) -q -k "$(P)" --no-header

test-security: ## Only the security/privacy regression tests
	$(PYTEST) -q -m "security"

fixtures: ## Regenerate fixture portfolios, expected snapshots and CASES.md
	$(GEN) --all
	$(CATALOG)

fixtures-check: ## Fail if the committed fixtures differ from their specs
	$(GEN) --check
	$(CATALOG) --check

fixtures-root: ## Assemble a runnable root from one fixture: make fixtures-root P=p2_equity_comp_rsu DIR=/tmp/ab
	@test -n "$(P)" || (echo "usage: make fixtures-root P=<slug> [DIR=<dir>]"; exit 2)
	$(GEN) --only $(P) --root $(or $(DIR),/tmp/advisorbot-fixture-root)
	@echo ""
	@echo "  Open it with:"
	@echo "    ADVISORBOT_ROOT=$(or $(DIR),/tmp/advisorbot-fixture-root) $(PYTHON) Scripts/holdings.py refresh"
	@echo "    ADVISORBOT_ROOT=$(or $(DIR),/tmp/advisorbot-fixture-root) $(PYTHON) Scripts/advisorbot_server.py --port 8799"

catalog: ## Rebuild Scripts/tests/CASES.md from the test docstrings
	$(CATALOG)

baselines: ## Refresh the shrink-only leak and fallback baselines after a round of fixes
	$(GEN) --all
	$(BASELINES)

snapshots: ## Rewrite expected snapshots from the current code (review the diff!)
	$(PYTEST) -q --update-snapshots

clean-pyc: ## Remove __pycache__ and .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache Scripts/tests/.pytest_cache

commit-template: ## Configure git to use the repo .gitmessage commit template
	git config commit.template .gitmessage
	@echo "Configured git commit.template -> .gitmessage"
