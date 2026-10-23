PY := backend/.venv/bin/python
PIP := backend/.venv/bin/pip
UVICORN := backend/.venv/bin/uvicorn
ALEMBIC := backend/.venv/bin/alembic
PYTEST := backend/.venv/bin/pytest

.DEFAULT_GOAL := help

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

install: ## Install backend deps and frontend deps
	cd backend && python3 -m venv .venv && $(PIP) install -q --upgrade pip && $(PIP) install -q -r requirements.txt
	cd frontend && npm install --silent

db: ## Create the local database if missing
	@psql -lqt | cut -d \| -f 1 | grep -qw ai_dj_studio && echo "database ai_dj_studio already exists" || createdb ai_dj_studio

migrate: ## Apply Alembic migrations
	cd backend && $(ALEMBIC) upgrade head

reset-db: ## Drop and recreate the database, then migrate (destroys all data)
	dropdb --if-exists ai_dj_studio && createdb ai_dj_studio
	$(MAKE) migrate

seed: ## Load demo users, tracks and lounge (idempotent)
	cd backend && $(PY) -m scripts.seed

api: ## Run the API server
	cd backend && $(UVICORN) app.main:app --reload --port 8000

web: ## Run the Vite dev server
	cd frontend && npm run dev

dev: ## Run API and frontend together
	$(MAKE) -j2 api web

test: ## Run the backend test suite
	cd backend && $(PYTEST) -q

test-v: ## Run tests with per-test output
	cd backend && $(PYTEST) -v

lint: ## Ruff + format check
	cd backend && ../backend/.venv/bin/ruff check app tests scripts

.PHONY: help install db migrate reset-db seed api web dev test test-v lint
