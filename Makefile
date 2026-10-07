PYTHON ?= python3
HOST ?= 0.0.0.0
PORT ?= 8000

.PHONY: install test lint format run docker-up docker-down

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

run:
	$(PYTHON) -m uvicorn app.main:app --reload --host $(HOST) --port $(PORT)

docker-up:
	docker compose up --build

docker-down:
	docker compose down
