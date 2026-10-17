# ComputeCoin — developer task runner.
# Run `make help` for the list.

.DEFAULT_GOAL := help
SHELL := /bin/bash

API_DIR := apps/api
WEB_DIR := apps/web
AGENT_DIR := provider-agent

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	 awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---- Infra ----
.PHONY: up down logs ps
up: ## Start the full local stack (db, redis, minio, api, web)
	docker compose up --build -d
	@echo "API:  http://localhost:8000/health   Web: http://localhost:3000"

down: ## Stop the stack
	docker compose down

logs: ## Tail stack logs
	docker compose logs -f

ps: ## Show stack status
	docker compose ps

infra: ## Start only backing services (db, redis, minio) for local app dev
	docker compose up -d db redis minio

# ---- API ----
.PHONY: api-install api-migrate api-run api-test
api-install: ## Install API deps into a venv
	cd $(API_DIR) && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e ".[dev]"

api-migrate: ## Apply DB migrations
	cd $(API_DIR) && .venv/bin/alembic upgrade head

api-run: ## Run the API locally
	cd $(API_DIR) && .venv/bin/uvicorn app.main:app --reload --port 8000

api-test: ## Run API tests
	cd $(API_DIR) && .venv/bin/pytest -q

# ---- Web ----
.PHONY: web-install web-dev web-build web-check
web-install: ## Install web deps
	cd $(WEB_DIR) && npm install

web-dev: ## Run the web app in dev mode
	cd $(WEB_DIR) && npm run dev

web-build: ## Production build of the web app
	cd $(WEB_DIR) && npm run build

web-check: ## Typecheck the web app
	cd $(WEB_DIR) && npm run typecheck

# ---- Provider agent ----
.PHONY: agent-install agent-test
agent-install: ## Install the provider agent
	cd $(AGENT_DIR) && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e ".[dev]"

agent-test: ## Test the provider agent
	cd $(AGENT_DIR) && .venv/bin/pytest -q

# ---- Aggregate ----
.PHONY: test
test: api-test agent-test ## Run all Python test suites
