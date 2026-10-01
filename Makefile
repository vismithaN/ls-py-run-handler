.PHONY: db-up db-down db-migrate db-downgrade db-migrate-test server format lint lint-fix test test-setup benchmark benchmark-compare benchmark-compare-save memprofile

POETRY ?= poetry

# Start all database services defined in docker-compose-db.yaml
db-up:
	docker compose -f docker-compose-db.yaml up -d --wait

# Stop all database services defined in docker-compose-db.yaml
db-down:
	docker compose -f docker-compose-db.yaml down

# Run database migrations
db-migrate:
	$(POETRY) run alembic upgrade head

# Run migrations with test environment
db-migrate-test:
	RUN_HANDLER_ENV=test $(POETRY) run alembic upgrade head

# Downgrade database to previous migration
db-downgrade:
	$(POETRY) run alembic downgrade -1

# Run migrations and start the server
server: db-migrate
	$(POETRY) run uvicorn ls_py_handler.main:app --reload

# Format code using Ruff
format:
	$(POETRY) run ruff format ls_py_handler tests scripts

# Lint code using Ruff
lint:
	$(POETRY) run ruff check ls_py_handler tests scripts

# Automatically fix linting issues using Ruff
lint-fix:
	$(POETRY) run ruff check --fix ls_py_handler tests scripts

# Set up test environment (reset database and S3 bucket)
test-setup:
	@echo "Setting up test environment..."
	RUN_HANDLER_ENV=test $(POETRY) run python scripts/reset_test_environment.py
	$(MAKE) db-migrate-test POETRY="$(POETRY)"
	@echo "Test environment setup complete!"

# Run tests with test environment
test: test-setup
	RUN_HANDLER_ENV=test $(POETRY) run pytest -s

# Run performance benchmarks
benchmark: test-setup
	@echo "Running performance benchmarks..."
	RUN_HANDLER_ENV=test $(POETRY) run pytest tests/benchmarks/test_run_performance.py -v --benchmark-save=baseline --benchmark-compare

# Run memory profiling
memprofile: test-setup
	@echo "Running memory profiling..."
	RUN_HANDLER_ENV=test $(POETRY) run pytest tests/benchmarks/test_run_performance.py -v --memray
