.PHONY: db-up db-down db-migrate db-downgrade db-migrate-test server format lint lint-fix test test-setup benchmark benchmark-compare benchmark-compare-save memprofile measure dashboard

# Options for the optional measurement harness only
MEASURE_ARGS ?= --stage baseline
DASHBOARD_ARGS ?=

# Start all database services defined in docker-compose-db.yaml
db-up:
	docker-compose -f docker-compose-db.yaml --profile postgres-15 up -d

# Stop all database services defined in docker-compose-db.yaml
db-down:
	docker-compose -f docker-compose-db.yaml down

# Run database migrations
db-migrate:
	poetry run alembic upgrade head

# Run migrations with test environment
db-migrate-test:
	RUN_HANDLER_ENV=test poetry run alembic upgrade head

# Downgrade database to previous migration
db-downgrade:
	poetry run alembic downgrade -1

# Run migrations and start the server
server: db-migrate
	poetry run uvicorn ls_py_handler.main:app --reload

# Format code using Ruff
format:
	poetry run ruff format ls_py_handler tests scripts

# Lint code using Ruff
lint:
	poetry run ruff check ls_py_handler tests scripts

# Automatically fix linting issues using Ruff
lint-fix:
	poetry run ruff check --fix ls_py_handler tests scripts

# Set up test environment (reset database and S3 bucket)
test-setup:
	docker-compose -f docker-compose-db.yaml exec -T db-postgres-15 psql -U postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS postgres_test;" -c "CREATE DATABASE postgres_test;"
	RUN_HANDLER_ENV=test poetry run python -m scripts.reset_test_bucket
	$(MAKE) db-migrate-test

# Run correctness tests with the test environment, excluding benchmarks
test: test-setup
	RUN_HANDLER_ENV=test poetry run pytest tests --ignore=tests/benchmarks -s

# Run performance benchmarks
benchmark: test-setup
	@echo "Running performance benchmarks..."
	RUN_HANDLER_ENV=test poetry run pytest tests/benchmarks/test_run_performance.py -v --benchmark-save=baseline --benchmark-save-data --benchmark-compare

# Run memory profiling; retain each run in a unique output directory
memprofile: test-setup
	@mkdir -p profiles/memory
	@memory_profile_dir=$$(mktemp -d "profiles/memory/$$(date -u +%Y%m%dT%H%M%SZ)-XXXXXXXX") && \
		echo "Saving memory profiles to $$memory_profile_dir" && \
		RUN_HANDLER_ENV=test poetry run pytest tests/benchmarks/test_run_performance.py -v --memray --memray-bin-path="$$memory_profile_dir"

# Optional supplementary measurements; resets test resources per workload/pass/session
measure:
	RUN_HANDLER_ENV=test poetry run python -m scripts.measure $(MEASURE_ARGS)

# Local viewer only; does not reset test resources or execute benchmarks
dashboard:
	poetry run python -m scripts.benchmark_dashboard $(DASHBOARD_ARGS)
