PY := $(CURDIR)/backend/.venv/bin/python
PIP := $(CURDIR)/backend/.venv/bin/pip
UVICORN := $(CURDIR)/backend/.venv/bin/uvicorn
ALEMBIC := $(CURDIR)/backend/.venv/bin/alembic
PYTEST := $(CURDIR)/backend/.venv/bin/pytest

.DEFAULT_GOAL := help

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

install: ## Install backend deps (and frontend deps once frontend/ exists)
	cd backend && python3 -m venv .venv && $(PIP) install -q --upgrade pip && $(PIP) install -q -r requirements.txt
	@test -d frontend && cd frontend && npm install --silent || echo "frontend/ not created yet (Week 3) — skipped"

db: ## Create the local database if missing
	@psql -lqt | cut -d \| -f 1 | grep -qw ai_dj_studio && echo "database ai_dj_studio already exists" || createdb ai_dj_studio

migrate: ## Apply Alembic migrations
	cd backend && $(ALEMBIC) upgrade head

reset-db: ## Drop and recreate the database, then migrate (destroys all data)
	dropdb --if-exists ai_dj_studio && createdb ai_dj_studio
	$(MAKE) migrate

seed: ## Load demo users, tracks and lounge (idempotent)
	cd backend && $(PY) -m scripts.seed

media: ## Synthesise the demo audio for every catalog track (PRD §4.2: no licensed music)
	cd backend && $(PY) -m scripts.synth_media

api: ## Run the API server
	cd backend && $(UVICORN) app.main:app --reload --port 8000

web: ## Run the Vite dev server
	cd frontend && npm run dev

dev: ## Run API and frontend together
	$(MAKE) -j2 api web

test: ## Run the backend test suite
	cd backend && $(PYTEST) -q

test-front: ## Run the console's reducer tests (vitest, node environment)
	cd frontend && npm test

test-v: ## Run tests with per-test output
	cd backend && $(PYTEST) -v

lint: ## Ruff check
	cd backend && $(PY) -m ruff check app tests scripts

eval-recs: ## Offline Precision@5 for the recommendation rail (PRD §10.3)
	cd backend && $(PY) -m scripts.rec_eval

load: ## §10.2 load test: a scripted crowd against the gateway (starts its own server)
	cd backend && $(PY) -m scripts.load_test

.PHONY: help install db migrate reset-db seed media api web dev test test-front test-v lint eval-recs load
