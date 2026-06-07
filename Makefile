.DEFAULT_GOAL := help
SHELL := /bin/bash
RUN := uv run

.PHONY: help install up down migrate seed test lint fmt mcp demo ui

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install (editable, with dev deps)
	uv venv
	uv pip install -e ".[dev]"

up:  ## Start Postgres + pgvector (docker compose up -d)
	docker compose up -d

down:  ## Stop services
	docker compose down

migrate:  ## Apply migrations/*.sql in order
	$(RUN) python -m mnemo.db

seed:  ## Load demo seed data
	@echo "seed: not yet — implemented in M1"; exit 1

test:  ## Run pytest
	$(RUN) pytest -q

lint:  ## ruff check + black --check
	$(RUN) ruff check .
	$(RUN) black --check .

fmt:  ## ruff --fix + black
	$(RUN) ruff check --fix .
	$(RUN) black .

mcp:  ## Run the MCP server (stdio)
	@echo "mcp: not yet — implemented in M5"; exit 1

demo:  ## Run the end-to-end rollback demo (spec §10)
	@echo "demo: not yet — implemented in M6"; exit 1

ui:  ## Run the FastAPI + HTMX web UI
	@echo "ui: not yet — implemented in M7"; exit 1
