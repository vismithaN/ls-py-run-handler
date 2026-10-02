# Correctness tests

`test_runs.py` contains eight tests: the original batch round trip, generated IDs,
large payloads, nested JSON/special characters, empty/omitted fields, repeated
content, invalid requests/missing runs, and a missing S3 object. There is no
parametrization expanding that count. The four existing performance benchmarks
remain separate and unchanged.

With local PostgreSQL and MinIO running, activate the project environment and run:

```sh
RUN_HANDLER_ENV=test python -m pytest tests/test_runs.py -v --tb=short
```

`conftest.py` handles setup separately from the test logic. It requires local
endpoints and CREATE DATABASE permission, creates a uniquely named database and
bucket, applies the existing migration, then removes only those temporary
resources at teardown. It does not reset existing test data. Run in one pytest
process; parallel execution is not configured. Avoid `make test-setup`, which
resets existing test storage.

The missing-object test requires an error response instead of a successful
response with incorrect empty fields. The original code is expected to fail
this assertion; it is deliberately not skipped. An exact error status policy
will be decided later. No application behavior is changed by these tests.

## Verification — 2026-10-02

Lint and formatting passed. Pytest collects exactly eight correctness tests
(twelve including the four unchanged benchmarks). Integration execution was
attempted but all eight tests were blocked at setup by a PostgreSQL connection
timeout on localhost:5432. No test body ran; no application passes or failures
are claimed yet.
