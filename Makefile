# =============================================================================
# Scout — developer entry points.
#
# `make` with no arguments prints the target list. Targets are documented with
# `##` comments; `make help` parses them, so keep that style.
#
# The fast local path is `make up` (PostgreSQL + Redis containers only); the
# Python applications run on the host through `make api|orchestrator|worker`,
# or in containers via `make up-app` (compose `app` profile).
#
# No secrets live here. Everything is read from the environment / `.env`.
# =============================================================================

SHELL := /bin/bash
# -e: stop at the first failing command inside a recipe line.
# -u: undefined variables are errors.
# -o pipefail: a failing command in a pipeline fails the line.
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

# --- Configuration -----------------------------------------------------------
COMPOSE_FILE   ?= infra/compose/docker-compose.yml
COMPOSE        ?= docker compose -f $(COMPOSE_FILE)
APP_PROFILE    ?= app
DATA_SERVICES  ?= postgres redis
APP_SERVICES   ?= api orchestrator worker
ALEMBIC_INI    ?= migrations/alembic.ini
UV             ?= uv
# uv projects and Python callers ignore `.env` on their own; load it for make.
# `include` is a no-op when the file is absent, so this stays safe on first run.
-include .env
PY_SOURCES     ?= packages apps/api apps/orchestrator apps/worker
# Marker expressions must match the markers registered in the root pyproject.
FAST_MARKERS   := not integration and not e2e and not sandbox
DASHBOARD_DIR  := apps/dashboard

# =============================================================================
# Help
# =============================================================================
.PHONY: help
help: ## Show this help (default target)
	@echo "Scout — available targets:"
	@echo ""
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Typical flow: bootstrap -> up -> migrate -> api"
	@echo "Full pre-commit gate: make verify"

# =============================================================================
# Bootstrap
# =============================================================================
.PHONY: bootstrap
bootstrap: .env ## Install Python (uv) and dashboard (pnpm) dependencies
	$(UV) sync --all-packages
	@if [ ! -f "$(DASHBOARD_DIR)/package.json" ]; then \
		echo "==> dashboard: $(DASHBOARD_DIR)/package.json not present yet; skipping pnpm install"; \
	else \
		echo "==> dashboard: corepack enable + pnpm install"; \
		corepack enable || echo "WARNING: 'corepack enable' failed; continuing"; \
		corepack prepare pnpm@9 --activate 2>/dev/null || true; \
		dashboard_status=0; \
		if command -v pnpm >/dev/null 2>&1; then \
			( cd "$(DASHBOARD_DIR)" && pnpm install ) || dashboard_status=$$?; \
		elif command -v corepack >/dev/null 2>&1; then \
			( cd "$(DASHBOARD_DIR)" && corepack pnpm install ) || dashboard_status=$$?; \
		else \
			echo "WARNING: neither pnpm nor corepack is available; skipping dashboard install"; \
			dashboard_status=1; \
		fi; \
		if [ "$$dashboard_status" -ne 0 ]; then \
			echo "WARNING: dashboard dependency install failed (exit $$dashboard_status)."; \
			echo "         The dashboard needs Node.js 20 LTS and pnpm 9 (see README requirements)."; \
			echo "         Current node: $$(node --version 2>/dev/null || echo 'not installed')."; \
			echo "         Python dependencies are installed; fix Node and re-run 'make bootstrap'."; \
		fi; \
	fi
	@echo "Bootstrap complete (Python workspace synced)."

# Create .env from the committed template on first run. `.env.example` holds no
# credentials; real secrets are supplied through the environment.
.PHONY: .env
.env:
	@if [ ! -f .env ]; then \
		echo "==> creating .env from .env.example (no secrets included)"; \
		cp .env.example .env; \
	fi

# =============================================================================
# Datastores — the fast path (no Python image builds)
# =============================================================================
.PHONY: up
up: .env ## Start PostgreSQL + Redis containers (fast path)
	$(COMPOSE) up -d $(DATA_SERVICES)
	@bash scripts/wait_for_services.sh

.PHONY: down
down: ## Stop and remove the compose containers (keeps volumes)
	$(COMPOSE) --profile $(APP_PROFILE) down --remove-orphans

.PHONY: logs
logs: ## Follow logs from all compose services
	$(COMPOSE) --profile $(APP_PROFILE) logs -f --tail=100

.PHONY: ps
ps: ## Show compose service status (including the app profile)
	$(COMPOSE) --profile $(APP_PROFILE) ps

# =============================================================================
# Full stack — includes the Python applications
# =============================================================================
.PHONY: up-app
up-app: .env ## Build and start the full stack (api, orchestrator, worker)
	$(COMPOSE) --profile $(APP_PROFILE) up -d --build
	@bash scripts/wait_for_services.sh

.PHONY: down-app
down-app: ## Stop the full stack and remove its containers
	$(COMPOSE) --profile $(APP_PROFILE) down --remove-orphans

# =============================================================================
# Database migrations (Alembic; migrations/ is owned by the persistence package)
# =============================================================================
.PHONY: migrate
migrate: ## Apply all database migrations (alembic upgrade head)
	$(UV) run alembic -c $(ALEMBIC_INI) upgrade head

.PHONY: migration
migration: ## Autogenerate a migration; requires m="message"
	@if [ -z "$(m)" ]; then \
		echo "ERROR: m is required, e.g. make migration m=\"add job evidence table\"" >&2; \
		exit 2; \
	fi
	$(UV) run alembic -c $(ALEMBIC_INI) revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Revert migrations; override with rev="-1" (default -1)
	$(UV) run alembic -c $(ALEMBIC_INI) downgrade $(or $(rev),-1)

.PHONY: db-reset
db-reset: ## Drop and recreate the schema, then re-apply migrations
	$(UV) run alembic -c $(ALEMBIC_INI) downgrade base
	$(UV) run alembic -c $(ALEMBIC_INI) upgrade head

# =============================================================================
# Run one process in the foreground, with reload
# =============================================================================
.PHONY: api
api: ## Run the FastAPI control plane on :8000 with reload
	$(UV) run uvicorn scout_api.main:app --host 0.0.0.0 --port 8000 --reload

.PHONY: orchestrator
orchestrator: ## Run the orchestrator in the foreground
	$(UV) run python -m scout_orchestrator

.PHONY: worker
worker: ## Run the worker in the foreground
	$(UV) run python -m scout_worker

.PHONY: dashboard
dashboard: ## Run the dashboard dev server (pnpm dev)
	@if [ ! -f "$(DASHBOARD_DIR)/package.json" ]; then \
		echo "ERROR: $(DASHBOARD_DIR)/package.json does not exist yet." >&2; \
		exit 1; \
	fi
	@if command -v pnpm >/dev/null 2>&1; then \
		cd "$(DASHBOARD_DIR)" && pnpm dev; \
	elif command -v corepack >/dev/null 2>&1; then \
		cd "$(DASHBOARD_DIR)" && corepack pnpm dev; \
	else \
		echo "ERROR: neither pnpm nor corepack is available." >&2; \
		exit 1; \
	fi

# =============================================================================
# Tests — markers come from [tool.pytest.ini_options] in pyproject.toml
# =============================================================================
.PHONY: test
test: ## Run unit + contract tests (no Docker required)
	$(UV) run pytest -m "$(FAST_MARKERS)"

.PHONY: test-unit
test-unit: ## Run unit tests only
	$(UV) run pytest -m "not integration and not e2e and not sandbox and not contract"

.PHONY: test-contract
test-contract: ## Run contract tests only
	$(UV) run pytest -m "contract"

.PHONY: test-integration
test-integration: ## Run integration tests (requires: make up)
	$(UV) run pytest -m "integration"

.PHONY: test-e2e
test-e2e: ## Run the end-to-end vertical slice (requires the full stack)
	$(UV) run pytest -m "e2e"

.PHONY: test-all
test-all: ## Run unit + contract + integration tests (requires: make up)
	$(UV) run pytest -m "not e2e and not sandbox"

.PHONY: test-sandbox
test-sandbox: ## Build the sandbox image and run sandbox tests (needs Docker)
	bash scripts/sandbox_smoke.sh
	$(UV) run pytest -m "sandbox"

# =============================================================================
# Quality gate
# =============================================================================
.PHONY: lint
lint: ## Check formatting and lint rules (ruff)
	$(UV) run ruff format --check $(PY_SOURCES)
	$(UV) run ruff check $(PY_SOURCES)

.PHONY: format
format: ## Apply formatting and safe lint fixes (ruff)
	$(UV) run ruff format $(PY_SOURCES)
	$(UV) run ruff check --fix $(PY_SOURCES)

.PHONY: typecheck
typecheck: ## Static type check (mypy, strict)
	$(UV) run mypy

.PHONY: check
check: lint typecheck test ## lint + typecheck + test

.PHONY: verify
verify: lint typecheck test ## Full pre-commit gate: lint + typecheck + test

.PHONY: fix
fix: format ## Alias for `format` (write formatting/lint fixes)
	@echo "Applied formatting and safe lint fixes."

# =============================================================================
# Housekeeping
# =============================================================================
.PHONY: clean
clean: ## Remove caches and the contents of var/
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .mypy_cache .ruff_cache .pytest_cache .coverage htmlcov coverage.xml
	rm -rf $(DASHBOARD_DIR)/dist $(DASHBOARD_DIR)/node_modules/.vite
	@if [ -d var ]; then \
		find var -mindepth 1 -not -name '.gitkeep' -exec rm -rf {} + ; \
	fi
	@echo "Cleaned caches and var/ contents."
