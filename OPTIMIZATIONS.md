# Run ingestion optimization

## Result

The optimized implementation makes ingestion linear in the encoded payload size,
reduces PostgreSQL work from one insert per run to one binary bulk copy per batch,
and reduces version-2 reads from three object-store range requests to one. On the
provided benchmark shapes, mean write latency improved by **57.3–85.7%** and mean
read latency improved by **89.2–93.8%**.

## What was slow and unsafe in the starter implementation

The original write path serialized the complete batch, serialized each large
field again, and then searched the complete batch for each serialized field. That
made offset discovery approach `O(run count × batch bytes)`. It was also incorrect
when the same JSON value appeared more than once: `bytes.find()` could return the
first occurrence instead of the occurrence belonging to a specific run.

It then executed one awaited PostgreSQL insert per run. A 500-run request therefore
paid 500 database round trips. PostgreSQL connections and S3 clients were also
created for each request. Reads opened another database connection and issued
three S3 range requests for `inputs`, `outputs`, and `metadata`.

Failures were broadly swallowed, which could return a successful response after a
partial write. The service had no request limits, dependency readiness check, or
stage-level timing data.

## New design

### Write path

```text
validated runs
      │
      ▼
encode each complete run once as NDJSON ──► exact byte start + length
      │
      ▼
one immutable S3 PutObject
      │
      ▼
one PostgreSQL transaction + binary COPY for every index row
```

Each complete run is one independently valid JSON record in an NDJSON object. The
encoder writes forward into a seekable `BytesIO` and records offsets from the
current position. It never scans previously encoded bytes, so its time complexity
is `O(total encoded bytes)`. Duplicate objects, Unicode, escape sequences, and
embedded newlines cannot confuse the offsets.

The object is uploaded before the relational index is committed, so a visible row
never points at an object that was never uploaded. If the PostgreSQL transaction
fails, the handler performs a best-effort compensating delete of the object and
returns an error. Duplicate IDs return `409`; storage dependency failures return
`503`; invalid/oversized requests return `4xx`.

PostgreSQL and S3 do not offer a shared transaction. A process crash after the S3
upload but before the database commit can still leave an unreferenced object. In a
larger production system, a periodic reconciler or object lifecycle rule should
delete old unreferenced batch objects. This is safer than committing the database
first, which could expose rows whose payload does not exist.

### Read path

```text
run UUID ──► one PostgreSQL primary-key lookup
                         │
                         ▼
              one S3 byte-range GET
                         │
                         ▼
                 decode + validate ID
```

Version-2 rows contain `object_key`, `byte_start`, and `byte_length`, so the full
run is fetched in one range request. The returned JSON is validated and its ID is
checked against the requested ID before being returned.

The migration keeps the three original reference columns and assigns existing
rows `format_version = 1`. Reads route those rows through the legacy three-range
decoder, allowing a rolling deployment without rewriting historical objects.

### Shared resources and bounds

- One bounded asyncpg pool is created for the application lifespan.
- One long-lived S3 client uses bounded HTTP connections, timeouts, and standard
  retries.
- Default limits are 1,000 runs, 64 MiB request bodies, and 64 MiB encoded batch
  objects. They are configurable through environment variables. An ingress proxy
  should enforce the same body limit before buffering traffic in a public deployment.
- `X-Request-ID` is accepted or generated and returned to callers.
- Structured logs contain request/batch IDs, sizes, stage timings, status codes,
  and error classes, but never run payloads.
- `/health/live` checks the process; `/health/ready` checks PostgreSQL and S3.

## Expected versus measured outcome

The targets were deliberately ranges because local object-storage and Docker
variance is meaningful. The actual numbers below are arithmetic means from
`pytest-benchmark` with at least three rounds and a five-second timing budget.
Both revisions used the same persistent event-loop harness; payload construction
and client-side JSON serialization were outside the timed section.

| Scenario | Success target | Starter mean | Optimized mean | Actual improvement |
|---|---:|---:|---:|---:|
| POST 500 runs × 10 KB per large field | 60–80% lower | 2,131.9 ms | 304.2 ms | **85.7% lower / 7.01×** |
| POST 50 runs × 100 KB per large field | 30–50% lower | 819.1 ms | 349.4 ms | **57.3% lower / 2.34×** |
| GET one 10 KB-per-field run | 50–75% lower | 106.6 ms | 6.6 ms | **93.8% lower / 16.24×** |
| GET one 100 KB-per-field run | 50–75% lower | 110.2 ms | 11.9 ms | **89.2% lower / 9.25×** |

Memray measured 500×10 KB allocation at the high-watermark falling from 170.4
MiB to 140.5 MiB (**17.5%**) and 50×100 KB falling from 157.4 MiB to 127.3 MiB
(**19.1%**). The remaining large allocations are principally unavoidable request
body parsing plus the single encoded upload buffer.

These are local comparative results, not production capacity claims. Before a
production rollout, run a concurrent load test against production-like PostgreSQL
and object storage and track p50/p95/p99 latency, error rate, pool wait time,
throughput, and orphan-cleanup count.

## Verification strategy

Automated tests cover:

- exact round trips for duplicate values, Unicode, escaped data, and newlines;
- exact byte-range decoding and encoded-size enforcement;
- empty, over-count, and oversized batches;
- atomic duplicate-ID behavior and compensating object deletion;
- injected S3 and PostgreSQL failures with explicit status codes;
- missing-object integrity errors;
- version-1 read compatibility;
- liveness and dependency readiness;
- the four supplied latency scenarios and both batch memory scenarios.

Run the complete workflow with:

```bash
make db-up
make test
make benchmark
make memprofile
```

`make test-setup` is guarded to reset only `postgres_test` and `runs-test`.

## Further production work

The highest-value next steps are a concurrent load test, database/S3 dashboards
and alerts, an orphan reconciler, authentication/authorization, and a streaming or
multipart upload design if the product needs batches materially larger than the
configured 64 MiB bound. Compression should be evaluated separately: it reduces
storage and transfer but makes direct byte-range access more complicated.
