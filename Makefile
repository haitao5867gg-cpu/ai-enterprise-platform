.PHONY: up up-full down logs test lint fmt eval

up:        ## Start the full local stack
	docker compose up --build -d
	@echo "Web: http://localhost:3000  API: http://localhost:8000/docs"

up-full:   ## Full stack plus self-hosted Langfuse; every run is traced
	LANGFUSE_PUBLIC_KEY=pk-lf-local-dev LANGFUSE_SECRET_KEY=sk-lf-local-dev \
	LANGFUSE_HOST=http://langfuse-web:3000 \
	docker compose --profile observability up --build -d
	@echo "Web: http://localhost:3000  API: http://localhost:8000/docs"
	@echo "Langfuse: http://localhost:3100 (admin@aep.local / aep-local-dev)"

down:      ## Stop the stack, Langfuse included (data volumes are kept)
	docker compose --profile observability down

logs:
	docker compose logs -f

# Score the ETL eval set against the running stack, e.g. `make eval MODEL=company`.
# Needs a real model; with Databricks configured it also checks the dev runs.
API ?= http://localhost:8000
eval:      ## Run evals/etl/cases.yaml and print the scores
	cd backend && uv run aep-eval ../evals/etl/cases.yaml --api $(API) $(if $(MODEL),--model $(MODEL)) $(if $(ONLY),--only $(ONLY))

test: test-connectors      ## Connector + backend tests + frontend type check
	cd backend && uv run pytest -q
	cd frontend && npm run typecheck

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .
	cd connectors/postgres && uv run ruff check . && uv run ruff format --check .
	cd connectors/mysql && uv run ruff check . && uv run ruff format --check .

fmt:
	cd backend && uv run ruff check --fix . && uv run ruff format .
	cd connectors/postgres && uv run ruff check --fix . && uv run ruff format .
	cd connectors/mysql && uv run ruff check --fix . && uv run ruff format .

.PHONY: test-connectors smoke-connectors

test-connectors:  ## Integration tests against the synthetic source database
	docker compose up -d --wait source-postgres source-mysql
	uv run --frozen --project connectors/postgres pytest connectors/postgres/tests -q
	uv run --frozen --project connectors/mysql pytest connectors/mysql/tests -q

smoke-connectors: ## Call all five tools on the running Streamable HTTP server
	uv run --frozen --project connectors/postgres aep-postgres-smoke
	uv run --frozen --project connectors/mysql aep-mysql-smoke
