# Optimization experiment table — draft

Start fresh from the original implementation (`b11a9b9`). Fill in expectations
before making each change and results after testing it. The optimization rows
below are candidates; we will choose their order after measuring the baseline.
Earlier optimization branches are not evidence for this fresh experiment.

Use the same four supplied workloads at baseline and after every optimization:
POST 500 × 10 KB per field, POST 50 × 100 KB per field, GET 10 KB per field,
and GET 100 KB per field. These are the benchmark scenarios. Keep the supplied
`make benchmark` and `make memprofile` workflows as the primary measurements;
`make measure` is an optional, separate harness for supplementary measurements.

## Correctness test results

Tests: [`tests/test_runs.py`](tests/test_runs.py). Executed: 2026-10-02.
**Original-code result: 7 passed, 1 failed** in 3.10 seconds, with two existing
FastAPI deprecation warnings. That run used application code from `b11a9b9`.

Docker recovered after closing its stalled processes and relaunching it; the Mac
had approximately 10 GiB free. The existing PostgreSQL and MinIO containers were
healthy. The documented test reset was performed on `postgres_test` and
`runs-test`, then the original migration was applied. Poetry was not on PATH and
the MinIO container lacked `mc`, so the same reset/migration steps used Docker
psql, the S3 API and the existing Python environment. Resetting the bucket removed
463 prior test objects; no backup was retained. No performance baseline was run.

Run: `RUN_HANDLER_ENV=test python -m pytest tests/test_runs.py -v --tb=short`
using the existing project environment (Python 3.11.9, pytest 8.3.5).
Service images: `postgres:15.10` and the existing
`ls-py-run-handler/minio:RELEASE.2025-10-15T17-29-55Z`. The Compose download of
`minio/minio` failed; the healthy local MinIO container was used instead.

| Test | Result | Expected vs actual | Decision |
|---|---|---|---|
| Batch round trip | Pass | Expected: all three runs match every submitted field, including explicit IDs and trace IDs. Actual: all comparisons passed. | Keep as a regression test. |
| Generated IDs | Pass | Expected: valid, unique IDs that retrieve the correct runs. Actual: both generated IDs and their run contents passed validation. | Keep as a regression test. |
| Large payload | Pass | Expected: approximately 100 KB per large field survives without truncation or changes. Actual: all three large fields matched exactly. | Keep as a regression test. |
| Nested JSON and special characters | Pass | Expected: nested values, Unicode and escaping are preserved. Actual: all submitted values matched exactly. | Keep as a regression test. |
| Empty and omitted fields | Pass | Expected: explicit empty dictionaries and omitted fields return empty dictionaries. Actual: both runs matched expected defaults. | Keep as a regression test. |
| Repeated field content | Pass | Expected: repeated/nested content within and across runs is retrieved correctly. Actual: both runs matched every field. | Keep as a regression test. |
| Invalid requests and missing runs | Pass | Expected: empty batch → `400`; missing required field → `422`; invalid GET UUID → `422`; nonexistent valid UUID → `404`; rejected POSTs persist no data. Actual: all statuses matched, with no added rows or objects. | Keep as a regression test. |
| Missing S3 object | Fail on original code; Pass after fix | Expected: error response. Original: GET returned `200` with all large fields `{}` after `NoSuchKey`. Fixed: GET returns `503` with an error message. | Fixed separately; retain original result for comparison. |

The confirmed defect is in `fetch_from_s3`: its exception handler substitutes
`{}`, allowing `get_run` to return a successful response with lost field values.
The correctness fix is separate from performance optimizations.

### Step log — 2026-10-02

| Step | Change | Verification |
|---|---|---|
| Reliable test command | Installed Poetry on PATH; replaced `mc` reset with S3 API. Setup commit: `b5df2f7`. | Clean setup completed; original code still reproduced 7 passes / 1 failure. |
| Correctness fix | Unreadable stored data now returns HTTP `503` instead of empty fields with `200`. Fix commit: `d363fb8`. | Correctness suite: **8 passed** in 8.70 seconds; two existing deprecation warnings. |
| Separate commands | `make test` excludes `tests/benchmarks`; `make benchmark` selects only benchmarks. Removed the extra correctness target. | `make test`: **8 passed** in 9.30 seconds. Benchmark selection: four cases collected; benchmarks not executed in this step. |
| Measurement harness | Added separate timing, Yappi CPU, Memray allocation and diagnostic passes. Saves raw samples, profiles, environment/code fingerprints and graph-ready CSV. No application optimizations or commits. | Correctness: **8 passed** in 12.34 seconds. Final smoke matrix: **16/16 workload/pass combinations**, **32/32 measured requests correct**. Verified readable profiles, exact body bytes, connection reuse/closures, retries, COPY/transactions and summary math. These checks are **not baseline results**. |
| Detachable harness | Restored the supplied `make benchmark` and `make memprofile` commands; supplementary collection is now opt-in through `make measure`. Do not mix their timing/memory result series. | Original benchmark/profile target definitions match the pre-harness version; command checks only, no new baseline measurements. |

Keep `b11a9b9` as the original reference. Identify the separate correctness-fix
revision when collecting the corrected performance baseline. No full-workload
performance baseline has been collected; tiny fixtures validate the harness only.

## Supplementary measurements — optional harness

**Keep the inputs and procedure deterministic. Performance results will vary.**
Use the same four workloads and measurement definitions before and after each
change. Retain individual request samples so summaries can be reproduced.
The definitions below describe `make measure`, not changes to the supplied
pytest benchmarks. Their timing, repetition and profiling scopes remain intact.
Collect a separate baseline for each workflow; never compare original pytest
numbers directly with harness numbers.

| Measurement | Unit | Exact definition | Should repeat exactly? |
|---|---|---|---|
| Median latency | ms/request | Middle value of sorted successful, correct request durations; average the two middle values for an even sample count | No—report variation across sessions |
| Mean latency | ms/request | Sum of successful, correct request durations divided by their count | No |
| p95 latency | ms/request | Sort successful, correct request durations; select position `ceil(0.95 × sample count)`, counting from 1. Always report the sample count and flag small-sample results as uncertain | No |
| Successful request/batch rate | requests/s | Correctly completed requests divided by elapsed time for the measured workload. One POST request is one batch; one GET request retrieves one run | No—label the traffic/concurrency used |
| Ingestion rate | runs/s | Correctly persisted runs divided by elapsed time for the measured workload; POST only | No |
| Peak allocated memory | MiB | Memray allocation high-watermark for the whole harness workload session, using identical scope. Includes in-process client/fixture allocations; this is not server-only RAM or RSS | No—collect separately from primary timing |
| CPU time | CPU seconds/workload and CPU seconds/request | Unprofiled Python process CPU time (user + system) before/after the measured timing-pass workload; divide by attempted requests. Includes the in-process client; excludes PostgreSQL/MinIO processes and Yappi overhead | No—keep failed-request counts visible |
| CPU profile | Saved function-level profile | Profile the same workload to identify expensive functions. Record profiler, version and timing mode; distinguish CPU execution from elapsed time waiting for I/O | No—compare where time is spent |
| Memory profile | Saved allocation profile | Use Memray to identify allocation locations alongside the peak allocated memory measurement | No—compare where allocations happen |
| PostgreSQL command count | dispatch attempts/request | asyncpg Connection execution attempts, including COPY and transaction commands, grouped by operation. Count retries; multi-statement/`executemany` dispatches count as one attempt, not individual rows or wire messages. Exclude startup, migrations and seeding | Usually, for fixed code/state without retries; investigate differences |
| S3 request count | attempts/request | Actual API attempts during request handling, grouped by operation such as PUT/GET/HEAD, including retries. Exclude startup and test seeding; mark retries unobserved if the collector cannot see them | Usually, without retries; investigate differences |
| PostgreSQL / S3 connections created | connections per measured workload, separately for each service | Newly established outbound connections during the workload. Reusing a connection does not increase this count; record startup/warm-up counts separately | Not guaranteed—reuse, scheduling and reconnects can change it |
| Peak open PostgreSQL / S3 connections | connections, separately for each service | Track successful connection openings and closures; retain the highest simultaneous open count, including connections retained from warm-up. Record event coverage; if sampled instead, label it an observed lower bound and record the interval | Not guaranteed |
| Payload size | bytes/request | Exact serialized POST body size; for GET, exact response body size. Record stored object bytes (POST) and fetched S3 bytes (GET) separately | Yes, with frozen fixtures/IDs and equivalent serialization; a format change may deliberately alter storage bytes |
| Correct completions / failures / timeouts | counts per measured workload | Count every attempted request in exactly one outcome; a successful HTTP status with wrong/incomplete data is a failure. Also verify persisted POST data | Expected to repeat without injected faults; any difference needs explanation |

**Harness request timing boundary:** start immediately before the in-process API call and
stop after its complete response body is received. Use a monotonic clock. Exclude
fixture generation, client-side JSON serialization, startup, seeding and warm-up.
Correctness verification outside that boundary must be the same for both versions.
Keep one event loop/application lifecycle per session; apply the same harness to
the original and candidate code. Label real-HTTP measurements separately.

## How we collect supplementary measurements

When using the optional harness, run the same passes for its baseline and after
every optimization. Each uses the
same workloads and equivalent starting data. Correctness checks apply to all
passes; run data verification outside the request timing boundary.

| Collection pass | How we collect it | Saved output |
|---|---|---|
| **Benchmark** | Time each complete API call; summarize correct completions and workload elapsed time. Also read the cheap process CPU clock before/after the workload | Individual timings, outcomes, mean/median/p95, requests/s, POST runs/s and unprofiled CPU seconds |
| **CPU** | Collect a Yappi function-level CPU profile in a separate pass | Profile artifact and instrumented CPU total; label profiling overhead separately |
| **Memory** | Track the whole workload session with Memray: fixture/client creation, startup, seed, warm-up, requests, verification and shutdown. Metadata collection/module imports are excluded. Use aggregated allocation capture to keep profile files manageable | Peak allocated MiB and allocation profile artifact |
| **Diagnostic** | Instrument database command execution and outgoing S3 HTTP attempts, successful connection establishments/closures, and actual serialized/transferred byte lengths | Per-operation counts, connections opened/peak open by service, payload/upload/download bytes |

Keep CPU profiling, memory profiling and detailed diagnostic instrumentation out
of the benchmark pass so their overhead does not distort headline latency.
Memory and CPU scope includes the in-process Python test client, not the
PostgreSQL or MinIO processes. Client creation and pool borrowing do not count
as opening physical connections. Exclude verification traffic from diagnostic
counts, just as we exclude startup, seeding and warm-up traffic.

**Implementation status:** the optional harness is
[`scripts/measure.py`](scripts/measure.py). It keeps one event loop/application
lifecycle/client per session. It is not imported or invoked by the supplied
benchmark or memory-profile commands. Its measurements form a separate series;
graph generation comes after collecting real results.

Commands:

```bash
make benchmark   # Original pytest-benchmark workflow
make memprofile  # Original pytest-memray workflow
make measure     # Optional harness: all four collection passes
```

Harness defaults: **3 independent sessions, 5 measured requests and 1 warm-up request**
per workload/pass. Choose and freeze the budget before collecting its baseline;
five samples do not establish a reliable tail percentile. Options include
`--requests`, `--sessions`, `--warmup`, `--workloads` and `--passes`.
`MEASURE_ARGS` affects **only `make measure`**, for example
`make measure MEASURE_ARGS="--stage baseline --passes diagnostic"`.

Each harness workload/pass/session resets **only `postgres_test` and `runs-test`** through
`make test-setup`. Do not run other writers against these resources concurrently.
POST verification reads **every persisted run** outside timing/CPU scope, so full
collections can take longer than the measured requests themselves.

Harness output: `measurements/<timestamp>/summary.csv`, plus per-session `result.json`,
`cpu.pstat`/`cpu.txt`, `memory.bin` and worker logs. Missing metrics remain empty/
`null`, not zero. Use only accepted **experiment** rows from the **benchmark**
pass for supplementary harness latency/throughput graphs. CSV also reports observed timing
overhead of instrumented passes versus timing-only runs; this includes noise.
`--smoke` uses tiny fixtures and labels results **harness-validation**, not baseline.

Diagnostics are validated on Python's selector loop with the recorded dependency
versions. SQL counts cover Connection APIs, not standalone PreparedStatement APIs.
S3 bytes distinguish attempted upload bodies, successful stored-object sizes and
download bodies consumed by the application; they exclude headers/TCP framing.

**Repeatability rules:**

- Freeze payloads, random seed, trace-ID/request sequence, GET fixture, concurrency,
  warm-up count, measured request count and timeout before the comparison.
  Like the supplied benchmarks, POST omits IDs: the server generates them.
  Preserve returned POST IDs/GET targets with results; request payloads and ID
  lengths are fixed, but generated UUID values and S3 object keys vary.
- Use equivalent starting database/object-store state and the same hardware,
  resource limits, Python/dependencies and PostgreSQL/MinIO configuration.
- Repeat independent sessions, alternating original/candidate order. Save the
  raw results from each session; choose the repetition budget before comparing.
- Label every result with optimization stage, source/harness revisions, workload,
  collection pass and session/repetition number. Record Python/dependency/Docker/
  service versions, CPU/RAM limits, cold/warm procedure, concurrency, timeouts,
  rounds and requests per round with results. Preserve profiles and raw samples
  alongside summaries so graphs can be regenerated.
- Keep instrumentation consistent across versions and document its overhead.
  Missing measurements are `Not measured`, never zero. A result is accepted
  only if correctness checks pass.

## Experiment results

| Experiment / what we change | What we measure | Expected outcome | Benchmark results: before → after |
|---|---|---|---|
| **Baseline — no changes** | All measurements defined above for the four supplied scenarios | Establish the starting numbers | Not measured |
| **Connection pooling** | POST/GET latency, new connections and peak open connections | Fewer new connections and lower request latency; bounded open connections | Not measured |
| **One-pass encoding** | POST latency, process CPU time, and peak allocated memory | Less repeated work, lower memory use, and faster ingestion | Not measured |
| **Bulk database writes** | POST latency, ingestion throughput and database command count | Fewer database commands and faster batch ingestion | Not measured |
| **One S3 read per run** | GET latency, S3 request count and downloaded bytes | Fewer S3 requests and faster retrieval; measure the byte-transfer tradeoff | Not measured |

The rows highlight each optimization's main measurements; collect the full
measurement set at every stage. Record expected effects before testing, including
a numeric target if justified; do not invent expected percentages.

For each result, include the workload and numbers—for example:
`POST 500 × 10 KB: median __ → __ ms; peak allocated memory __ → __ MiB`.
Check that the data remains correct before accepting an improvement.

## Final graphs and explanation

Use the original pytest-benchmark results for primary latency/throughput graphs
and the original pytest-memray profiles for memory comparisons. Use harness
results for supplementary graphs, labelled with their collection pass and scope.
Do not combine timings or memory values from different workflows into one
before/after series.

| Optimization | Main before/after graphs | Evidence explaining the result |
|---|---|---|
| Connection pooling | POST/GET latency; new connections by service | Peak open connections and connection reuse |
| One-pass encoding | POST latency; peak allocated memory; CPU time | CPU and allocation profiles showing where work changed |
| Bulk database writes | POST latency; POST runs/s | PostgreSQL commands by operation |
| One S3 read per run | GET latency; S3 requests | Downloaded bytes and request-count changes |
| Overall | Baseline → each optimization → final version, separately by workload and metric | Correctness/failure counts and observed tradeoffs throughout |

- Show absolute values and percentage changes, with variation across repeated
  sessions. State the repetition count and what any error bars represent.
- Compare each stage with its immediately preceding stage for incremental
  effect, and with the original baseline for cumulative effect. This attributes
  changes within that sequence; it does not prove optimizations are independent.
- For lower-is-better metrics, reduction is `(before − after) / before × 100`;
  for throughput, increase is `(after − before) / before × 100`. If the baseline
  value is zero, show the absolute change and mark the percentage `N/A`.
- Keep workloads, workflows and collection passes separate. Within the optional
  harness, use benchmark-pass timings for latency graphs, not profiler timings.
  Label sequential throughput as such;
  it is not evidence of maximum concurrent capacity.
- Explain each result as: **expected effect → actual numbers → supporting
  evidence → tradeoffs**. Include regressions and unchanged results, not just wins.



The sequence is:
1. Add correctness tests against the original implementation: POST → GET round trips, exact field values, batch integrity, and edge cases such as repeated field contents.
2. Run them on the unchanged code. If something fails, record it as an existing bug—don’t silently fix it and call that the original baseline.
3. Add and validate the optional measurement harness without changing the supplied benchmarks.
4. Collect the primary baseline with `make benchmark` and `make memprofile`; collect a separate supplementary baseline with `make measure` if used.
5. Apply one optimization, rerun correctness tests, then repeat measurements.
