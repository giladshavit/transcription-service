.PHONY: help install run test lint format

help: ## Show the available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install dependencies into .venv (needs uv)
	uv sync

run: ## Run the service on http://localhost:8000
	uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

test: ## Run the fast test suite
	uv run pytest

lint: ## Check formatting and common mistakes
	uv run ruff check .
	uv run ruff format --check .

format: ## Fix formatting and auto-fixable lint findings
	uv run ruff format .
	uv run ruff check --fix .
