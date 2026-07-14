.DEFAULT_GOAL := help
SHELL := /bin/bash
RUN := uv run

.PHONY: help install up down migrate seed test lint fmt mcp agents css demo ui export import test-db

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

lint:  ## ruff check + black --check
	$(RUN) ruff check .
	$(RUN) black --check .

fmt:  ## ruff --fix + black
	$(RUN) ruff check --fix .
	$(RUN) black .

mcp:  ## Run the six guarded memory tools over MCP (stdio)
	$(RUN) python -m mnemo.mcp_direct

agents:  ## Install the mnemo tool from this checkout, then connect Claude Code and Codex
	uv tool install --force --reinstall --python 3.12 .
	"$$(uv tool dir --bin)/mnemo" install $(ARGS)

demo:  ## Direct write -> history -> revert through the SDK, no models
	MNEMO_BACKEND=hash $(RUN) python -m examples.direct_memory

export:  ## Export the configured scope as versioned JSON (OUT=file, default stdout)
	$(RUN) python -m mnemo.transfer export --out $(or $(OUT),-)

import:  ## Restore an export into an empty, migrated store (FILE=export.json)
	$(RUN) python -m mnemo.transfer import $(FILE)

ui:  ## Run the FastAPI + HTMX web UI
	$(RUN) uvicorn web.app:app --host 127.0.0.1 --port 8000

css:  ## Rebuild web/static/app.css from the templates (needs Node for npx)
	npx -y tailwindcss@3.4.19 -c web/tailwind.config.js -i web/tailwind.css -o web/static/app.css --minify

test-db:  ## Required Postgres correctness checks
	MNEMO_REQUIRE_DB=1 $(RUN) pytest -q
