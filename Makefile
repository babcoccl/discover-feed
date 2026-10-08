PYTHON ?= python3
HOST ?= 0.0.0.0
PORT ?= 8000
DEMO_HOST ?= 127.0.0.1

.PHONY: install test lint format run demo docker-up docker-down

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

# Throwaway demo DB (.demo/) seeded from the example profiles + fixture feeds; no network.
# Fake LLM by default; real endpoint: make demo DEMO_ARGS="--real-llm --model <name>"
demo:
	$(PYTHON) -m app.demo --host $(DEMO_HOST) --port $(PORT) $(DEMO_ARGS)

docker-up:
	docker compose up --build

docker-down:
	docker compose down
