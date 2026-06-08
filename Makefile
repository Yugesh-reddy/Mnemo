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

demo:  ## Run the end-to-end rollback demo (spec §10)
	$(RUN) python examples/agent.py demo

ui:  ## Run the FastAPI + HTMX web UI
	$(RUN) uvicorn web.app:app --host 127.0.0.1 --port 8000

gif:  ## Record the rollback demo GIF (needs `brew install vhs`)
	vhs docs/demo.tape
