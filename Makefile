.DEFAULT_GOAL := help
SHELL := /bin/bash
RUN := uv run

.PHONY: help install up down migrate seed test lint fmt mcp mcp-direct demo demo-direct ui export import

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install (editable, with dev deps)
	uv sync --locked --extra dev

up:  ## Start Postgres + pgvector and wait for database health
	docker compose up -d --wait

down:  ## Stop services
	docker compose down

migrate:  ## Apply migrations/*.sql in order
	$(RUN) python -m mnemo.db

seed:  ## Load demo seed data
	$(RUN) python -m mnemo.seed

test:  ## Run pytest
	$(RUN) pytest -q

eval:  ## Precision/recall junk-rate eval — the north star
	$(RUN) python -m mnemo.eval

lint:  ## ruff check + black --check
	$(RUN) ruff check .
	$(RUN) black --check .

fmt:  ## ruff --fix + black
	$(RUN) ruff check --fix .
	$(RUN) black .

mcp:  ## Run the MCP server (stdio)
	$(RUN) python -m mnemo.mcp_server

mcp-direct:  ## Run six guarded memory tools over MCP (no extraction or decay)
	$(RUN) python -m mnemo.mcp_direct

demo:  ## Run the end-to-end rollback demo (spec §10)
	$(RUN) python examples/agent.py demo

demo-direct:  ## Direct write -> history -> revert through the SDK, no models
	MNEMO_BACKEND=hash MNEMO_WORKER_ENABLED=false $(RUN) python -m examples.direct_memory

export:  ## Export the configured scope as versioned JSON (OUT=file, default stdout)
	$(RUN) python -m mnemo.transfer export --out $(or $(OUT),-)

import:  ## Restore an export into an empty, migrated store (FILE=export.json)
	$(RUN) python -m mnemo.transfer import $(FILE)

ui:  ## Run the FastAPI + HTMX web UI
	$(RUN) uvicorn web.app:app --host 127.0.0.1 --port 8000

worker:  ## Run extraction with lease renewal and scheduled decay
	$(RUN) python -m mnemo.worker

health:  ## Print scoped queue health and archival activity
	$(RUN) python -m mnemo.worker --health

test-db:  ## Required Postgres correctness checks
	MNEMO_REQUIRE_DB=1 $(RUN) pytest -q
