.PHONY: up up-full down logs test lint fmt

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

test:      ## Backend tests + frontend type check
	cd backend && uv run pytest -q
	cd frontend && npm run typecheck

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .

fmt:
	cd backend && uv run ruff check --fix . && uv run ruff format .
