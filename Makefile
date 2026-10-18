.PHONY: install up down migrate logs format lint typecheck test test-kubernetes go-test go-check check clean web-install web-dev web-build chaos-list chaos-results demo benchmark

install:
	uv sync --locked --extra dev

up:
	docker compose --profile shopstack --profile observability up --build -d

down:
	docker compose down

migrate:
	docker compose run --rm api alembic upgrade head

logs:
	docker compose logs -f

format:
	uv run ruff format .

lint:
	uv run ruff check .

typecheck:
	uv run mypy apps packages tests

test:
	uv run pytest

go-test:
	cd apps/event-ingestion && go test ./...

go-check:
	cd apps/event-ingestion && test -z "$$(gofmt -l *.go)" && go vet ./... && go test ./...

test-kubernetes:
	OPSMIND_KUBERNETES_INTEGRATION=1 uv run pytest tests/integration/test_kubernetes_adapter_cluster.py -q

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy apps packages tests
	uv run pytest
	$(MAKE) go-check

clean:
	docker compose down --remove-orphans

web-install:
	cd apps/web && npm ci

web-dev:
	cd apps/web && npm run dev

web-build:
	cd apps/web && npm run build

chaos-list:
	uv run opsmind chaos list

chaos-results:
	uv run opsmind chaos results

demo:
	uv run opsmind demo run

benchmark:
	uv run python scripts/benchmark_opsmind.py --sizes 10,100 --concurrency 20
