.PHONY: up down test lint fmt recompute

up:
	docker compose up -d

down:
	docker compose down

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check src tests migrations

fmt:
	.venv/bin/ruff format src tests

recompute:
	.venv/bin/flowlens recompute --all

db-up:
	brew services start postgresql@16

db-down:
	brew services stop postgresql@16

db-reset:
	dropdb --if-exists flowlens && createdb flowlens && .venv/bin/alembic upgrade head
