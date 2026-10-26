.PHONY: up down logs test lint fmt

up:        ## Start the full local stack
	docker compose up --build -d
	@echo "Web: http://localhost:3000  API: http://localhost:8000/docs"

down:      ## Stop the stack (data volume is kept)
	docker compose down

logs:
	docker compose logs -f

test:      ## Backend tests + frontend type check
	cd backend && uv run pytest -q
	cd frontend && npm run typecheck

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .

fmt:
	cd backend && uv run ruff check --fix . && uv run ruff format .
