# Pre-baseline correctness tests

These 25 cases exercise the original implementation at `b11a9b9`. No application,
schema, dependency or performance-harness changes are included.

## Run

Start the project's local PostgreSQL and MinIO services first. Activate the
project's existing Python environment, then run from the repository root:

```sh
RUN_HANDLER_ENV=test python -m pytest tests/correctness -v -s --tb=short \
  -o junit_family=xunit1 --junitxml=/tmp/ls-run-correctness.xml
```

The suite requires local endpoints and a PostgreSQL user with CREATE DATABASE
permission. It creates a unique `correctness_<uuid>` database, applies the real
Alembic migration, and creates a unique `correctness-<uuid>` bucket. Teardown
deletes only those newly created resources, including their test rows/objects.
Existing databases and buckets are not reset. Do not use `make test` or
`make test-setup` for this isolated run; those targets reset existing test storage.
Use a single pytest process; parallel execution is not configured for these tests.

## Coverage

| Group | Cases | Assertions / observations |
|---|---:|---|
| Round trips and IDs | 3 | Every public field matches; explicit IDs survive; generated IDs are valid and unique across requests |
| Supplied POST workload sizes | 2 | 500 × 10 KB and 50 × 100 KB per field; retrieve and compare every run |
| JSON, Unicode, defaults and repeated content | 5 | Nested types, empty fields, escaping, multibyte strings, independent defaults and repeated/nested field values |
| Rejected input and missing records | 11 | Expected 400/422/404; rejected POSTs create neither rows nor objects |
| Storage failures | 4 | Upload failure before object creation, second-insert failure, missing object and invalid JSON at valid byte ranges |

Workload values/explicit UUIDs are deterministic; isolation resource names and
server-generated IDs are intentionally unique. Large-field dictionary structure
matches the supplied benchmark generator; these are correctness checks, not
performance measurements.

The mid-batch database failure is a **characterization test**: its passing status
means the fault was exercised and observations recorded, not that atomicity is
correct. It records persisted-run and remaining-object counts in terminal output
and JUnit properties. It does not assert that partial writes must be preserved.
The injection targets the baseline's sequential inserts and will need adaptation
when COPY is introduced.

The two damaged-object tests assert that GET must not return successful fabricated
data. Code inspection predicts failures because the original implementation
catches read/JSON errors and substitutes `{}`. They deliberately remain ordinary
assertions, not skipped tests. Confirm any failures by execution before recording
them as observed defects; agree on exact error codes in the later error-policy step.

## Verification status — 2026-10-02

- Ruff lint and formatting checks passed; pytest collected 25 cases.
- Integration execution was attempted but stopped during fixture setup:
  PostgreSQL on localhost:5432 timed out before any test body executed.
- Consequently, no integration passes or observed application failures are claimed.
- No baseline performance measurements have been collected by this suite.
