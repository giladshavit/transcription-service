.PHONY: help install run test test-integration lint format docker-up docker-down docker-test smoke

help: ## Show the available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

# ---- Docker (nothing else to install) ----

docker-up: ## Build the image and start the service on http://localhost:8000
	docker compose up --build

docker-down: ## Stop the service (add ARGS=-v to also delete its data)
	docker compose down $(ARGS)

docker-test: ## Run lint and the test suite inside the image
	docker compose run --rm --build tests

smoke: ## Upload the sample recording to the running service and print the transcript
	./scripts/smoke_test.sh

# ---- Local development (needs Python 3.12+, uv and ffmpeg) ----

install: ## Install the locked dependencies into .venv
	uv sync

run: ## Run the service on http://localhost:8000
	uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

test: ## Run the fast test suite (uses a fake model)
	uv run pytest

test-integration: ## Run the tests that use the real model (downloads it on first use)
	uv run pytest -m integration

lint: ## Check formatting, common mistakes and types
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format: ## Fix formatting and auto-fixable lint findings
	uv run ruff format .
	uv run ruff check --fix .
