# SANKALP task runner.
#
# PY is overridable because this project is developed on a machine where the
# working interpreter is Anaconda's and a bare `python` resolves to the Windows
# Store stub. Override on the command line if yours differs:
#     make test PY=python3

PY ?= python

.PHONY: help test test-cov lint secrets corpus eval eval-stage4 eval-stage4-smoke eval-stage4-record verify

help:
	@echo "test                 run the full test suite"
	@echo "test-cov             run tests with coverage over core/"
	@echo "lint                 ruff + mypy (strict on core/)"
	@echo "secrets              fail if a live-looking API key is in the tree"
	@echo "corpus               regenerate the corpus and re-lock its hashes"
	@echo "eval                 Stage 3 evaluation (deterministic, no API key)"
	@echo "eval-stage4          Stage 4 compiler eval, replaying the committed cache"
	@echo "eval-stage4-smoke    Stage 4 on 3 seeds against the live API (cheap check)"
	@echo "eval-stage4-record   Stage 4 full train split against the live API (SPENDS MONEY)"
	@echo "verify               secrets + test + lint + Stage 3 eval"

secrets:
	$(PY) scripts/check_no_secrets.py

test:
	$(PY) -m pytest -q

test-cov:
	$(PY) -m pytest --cov=core --cov-report=term-missing

lint:
	$(PY) -m ruff check core eval agent scripts tests api mcp_server
	$(PY) -m mypy core

corpus:
	$(PY) -c "from pathlib import Path; from eval.generator import write_corpus; print(write_corpus(Path('eval/corpus')))"

eval:
	$(PY) -c "from eval.harness import write_results; write_results(); print('wrote eval/results/stage3_results.{json,md}')"

eval-stage4:
	$(PY) -m eval.compiler_harness --cache-only

# Both of the below need GROQ_API_KEY in .env (see .env.example).
# Records into eval/llm_cache/, which should then be committed so the run is
# reproducible without a key.
eval-stage4-smoke:
	$(PY) -m eval.compiler_harness --limit-seeds 3

eval-stage4-record:
	$(PY) -m eval.compiler_harness

verify: secrets test lint eval
