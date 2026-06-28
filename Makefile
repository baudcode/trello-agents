.PHONY: install lint test run format stop

install:
	uv sync --all-extras

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

format:
	uv run ruff check --fix src/ tests/
	uv run ruff format src/ tests/

test:
	uv run pytest -x -q

test-verbose:
	uv run pytest -v

stop:
	-lsof -ti:8000 | xargs kill -9 2>/dev/null; sleep 1

run: stop
	uv run uvicorn agents_trello.app:app --factory --host 0.0.0.0 --port 8000 --reload
