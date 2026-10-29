.PHONY: up up-full down logs test lint fmt eval demo

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

# Prepare the ETL demo on the running stack (docs/api/data-nodes.md): the MySQL sample
# database retail (a new volume loads it by itself), the two knowledge bases from
# demo/knowledge and the app retail-etl. Running it again changes nothing.
RETAIL_SQL = connectors/mysql/seed/003-retail.sql
SOURCE_MYSQL = docker compose exec -T source-mysql \
	sh -c 'MYSQL_PWD="$$MYSQL_ROOT_PASSWORD" exec mysql -uroot "$$@"' mysql
demo:      ## Load the retail sample, the demo knowledge bases and the app retail-etl
	@command -v uv >/dev/null || { echo "✗ 需要 uv（macOS：brew install uv）"; exit 1; }
	@found=$$($(SOURCE_MYSQL) -N -e "SHOW DATABASES LIKE 'retail'") || \
		{ echo "✗ 连不上 MySQL 示例库（source-mysql），先 make up"; exit 1; }; \
	if [ -z "$$found" ]; then \
		echo "导入 MySQL 示例库 retail"; $(SOURCE_MYSQL) < $(RETAIL_SQL) || exit 1; \
	else \
		echo "MySQL 示例库已有 retail 库"; \
	fi
	cd backend && uv run aep-demo --api $(API) $(if $(WEB),--web $(WEB))

test: test-connectors      ## Connector + backend tests + frontend type check
	cd backend && uv run pytest -q
	cd frontend && npm run typecheck

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .
	cd connectors/postgres && uv run ruff check . && uv run ruff format --check .
	cd connectors/mysql && uv run ruff check . && uv run ruff format --check .
	cd connectors/files && uv run ruff check . && uv run ruff format --check .
	cd connectors/sandbox && uv run ruff check . && uv run ruff format --check .

fmt:
	cd backend && uv run ruff check --fix . && uv run ruff format .
	cd connectors/postgres && uv run ruff check --fix . && uv run ruff format .
	cd connectors/mysql && uv run ruff check --fix . && uv run ruff format .
	cd connectors/files && uv run ruff check --fix . && uv run ruff format .
	cd connectors/sandbox && uv run ruff check --fix . && uv run ruff format .

.PHONY: test-connectors smoke-connectors

test-connectors:  ## Integration tests against the synthetic source database
	docker compose up -d --wait source-postgres source-mysql
	uv run --frozen --project connectors/postgres pytest connectors/postgres/tests -q
	uv run --frozen --project connectors/mysql pytest connectors/mysql/tests -q
	uv run --frozen --project connectors/files pytest connectors/files/tests -q
	uv run --frozen --project connectors/sandbox pytest connectors/sandbox/tests -q

smoke-connectors: ## Call all five tools on the running Streamable HTTP server
	uv run --frozen --project connectors/postgres aep-postgres-smoke
	uv run --frozen --project connectors/mysql aep-mysql-smoke
