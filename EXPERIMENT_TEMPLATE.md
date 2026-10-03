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
| Persistent memory profiles | `make memprofile` now saves binaries in a unique `profiles/memory/<UTC timestamp>-<random suffix>/` directory. Only the output destination changes; profiling scope and benchmarks stay the same. Generated profiles are Git-ignored. | Verified with isolated storage checks, without resetting the test resources used by the running baseline. |
| CPU profile folder | CPU profiles now go to `profiles/cpu/<run>/session-N/<workload>/`. Existing baseline profiles are preserved there with compatibility links at their old locations. Output location only; Yappi settings, workloads and timing boundaries are unchanged. The running baseline records the storage-only harness revision change. | Validated output routing and readable profiles without interrupting the baseline. |
| Clear pass names | Renamed the supplementary harness pass from `benchmark` to `cpu_time`; `make benchmark` still means the original pytest benchmark. Measurements are unchanged; existing baseline files keep their historical labels. | Checked command help and summary generation for both new and historical labels; no test-resource reset. |
| Completed baseline | Collected the original timing and memory workflows, plus supplementary CPU time, CPU profiles and diagnostics on `bdf1239` (includes the separate correctness fix). | Original timing: **4 passed**; original memory: **4 passed**. Supplementary: **36/36 combinations**, **180/180 measured requests correct**, and **12 readable CPU profiles**. Results: `measurements/baseline-bdf1239-gxSv1A8k/`. |
| Benchmark dashboard | Added a separate local viewer for `.benchmarks/`: all numeric statistics, raw timing distributions, stage names and SVG export. New completed reports are discovered every 3 seconds. No application or benchmark-method changes. | **2 dashboard tests passed**. Browser checks passed for live updates, stage labels, comparison sets, SVG export and 320–1440 px layouts. No PostgreSQL/S3 reset or benchmark execution. |
| Persisted baseline interpretation | Added the **Profile interpretation** tab: numeric CPU/memory results, expandable allocation details and a concise findings table. The initial CPU display used the warm capture; it was superseded below. | **2 dashboard tests passed**; 16 CPU rows / 4 memory peaks matched recordings. Browser: light/dark themes, details and all 20 benchmark charts verified. |
| Cold CPU interpretation | Replaced the tab’s warm CPU numbers with the 12 fresh-process cold captures. Shows import, startup and first-request CPU medians separately; updated function numbers, findings and source links. Memory values remain from their original capture. | **12/12 cold captures accepted**; 1 request/process, 0 warm-ups. Four phase medians and 16 function rows checked against recordings. **2 dashboard tests passed**; browser shows cold values. No application optimization or commit. |
| Cold CPU command | `make measure MEASURE_ARGS="--passes cpu"` now profiles imports, startup and one first request per fresh process, without warm-up. Existing warm profiles and the benchmark/memory commands are unchanged. | **4/4 smoke captures passed**; distinct processes, one endpoint call each, readable profiles and temporary-storage cleanup verified. Injected-failure cleanup and warm/cold comparison checks passed. Results: `measurements/cpu-cold-command-check/20261003T053611.281220Z/`. These are harness checks, not new baseline numbers. |
| Profile source lock | Pinned only the Yappi cold-CPU and original `make memprofile` Memray inputs used by the profile tab, with paths, byte sizes, SHA-256 hashes and calculation rules. The dashboard flags missing/changed files. | **14/14 source files verified**; dashboard tests include same-size content changes. No profiler or benchmark rerun. |

### Step log — 2026-10-03

| Step | Change | Verification |
|---|---|---|
| One-pass POST offsets | Build the same batch JSON in a byte buffer and record each large field's offset as it is written. No connection, database-write or GET change. Committed as `8f5b534`. | **10/10 tests passed**; **4/4 original benchmarks passed**; **4/4 original Memray profiles passed**; **12/12 cold CPU captures correct**. Reports are listed below. |
| S3 session experiment | Reuse the aiobotocore session, but still create and close an S3 client for each request. This isolates SDK setup from connection pooling and keeps the original benchmark loop behavior testable. | **13/13 tests passed** (10 run tests and 3 dashboard tests); **4/4 original benchmarks**, **4/4 original Memray profiles**, **12/12 cold CPU captures**, and **12/12 diagnostics** passed. Diagnostic requests: **60/60 correct**; S3 request and connection counts unchanged. One original benchmark report per stage so far. |

Keep `b11a9b9` as the original reference. Identify the separate correctness-fix
revision when collecting the corrected performance baseline. The full-workload
baseline is now collected; earlier tiny-fixture runs validate the harness only.

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
| CPU time | CPU seconds/workload and CPU seconds/request | Unprofiled Python process CPU time (user + system) before/after the measured `cpu_time` workload; divide by attempted requests. Includes the in-process client; excludes PostgreSQL/MinIO processes and Yappi overhead | No—keep failed-request counts visible |
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

For the active experiment, use only the `cpu` pass in this harness. The other
passes and their saved historical reports remain available if a later question
needs them; they are not inputs to the profile dashboard. When using any optional
pass for a comparison, run that same pass before and after the change. Each uses the
same workloads and equivalent starting data. Correctness checks apply to all
passes; run data verification outside the request timing boundary.

| Collection pass | How we collect it | Saved output |
|---|---|---|
| **CPU time (`cpu_time`)** | Read the process CPU clock before/after the workload, without profiling. Also retain supplementary request timings and rates; these are not the original pytest benchmark | Unprofiled CPU seconds, individual timings, outcomes, mean/median/p95, requests/s and POST runs/s |
| **CPU** | Collect a cold Yappi CPU profile: application imports, startup and one first request in a fresh process, without warm-up | Function profile plus CPU totals split into imports, startup/client and first request. Includes profiler overhead; not a warmed benchmark |
| **Memory** | Track the whole workload session with Memray: fixture/client creation, startup, seed, warm-up, requests, verification and shutdown. Metadata collection/module imports are excluded. Use aggregated allocation capture to keep profile files manageable | Peak allocated MiB and allocation profile artifact |
| **Diagnostic** | Instrument database command execution and outgoing S3 HTTP attempts, successful connection establishments/closures, and actual serialized/transferred byte lengths | Per-operation counts, connections opened/peak open by service, payload/upload/download bytes |

Keep CPU profiling, memory profiling and detailed diagnostic instrumentation out
of the `cpu_time` pass so their overhead does not distort its measurements.
Memory and CPU scope includes the in-process Python test client, not the
PostgreSQL or MinIO processes. Client creation and pool borrowing do not count
as opening physical connections. Exclude verification traffic from diagnostic
counts, just as we exclude startup, seeding and warm-up traffic.

**Implementation status:** the optional harness is
[`scripts/measure.py`](scripts/measure.py). It keeps one event loop/application
lifecycle/client per session. It is not imported or invoked by the supplied
benchmark or memory-profile commands. Its measurements form a separate series;
graph generation comes after collecting real results.

### `make measure` arguments

`--passes` selects which supplementary measurement to run. The four valid pass
names are:

| Argument | Question it answers | Tool / method |
|---|---|---|
| `cpu_time` | How much CPU did the workload use without profiler overhead? | Process CPU clock |
| `cpu` | Which functions used the CPU? | Yappi CPU profiler |
| `memory` | Where was memory allocated, and what was the peak? | Memray |
| `diagnostic` | How many PostgreSQL commands, S3 requests, connections and bytes were used? | Harness instrumentation |

Running `make measure` without `--passes` runs all four passes, so use an
explicit selection for the active experiment:

```bash
make benchmark   # Original pytest-benchmark workflow
make memprofile  # Original pytest-memray workflow
make measure MEASURE_ARGS="--passes cpu"
```

Use `cpu` to explain where CPU time went; its timings include profiler overhead
and are not benchmark results. Keep `cpu_time` in reserve if a later experiment
needs unprofiled process CPU time. Do not mix the optional `memory` pass with the
original `make memprofile` memory series. The original benchmark and memory
workflows remain separate from this supplementary harness.

For readable benchmark graphs, run `make dashboard` and open
`http://127.0.0.1:8765`. In another terminal, run
`PYTEST_ADDOPTS="--benchmark-save-data" make benchmark` to retain raw samples as
well as summary statistics. The dashboard adds completed reports automatically;
rename each run to its optimization stage in the page. Labels persist separately
in `.benchmarks/.dashboard-labels.json`; original reports are never edited.
Plain `make benchmark` still updates summary graphs, but may omit raw samples.
Different recorded environments/options are not mixed. Each report is one run;
repeated runs are not automatically averaged. The separate **Profile interpretation**
tab shows saved Yappi CPU and original Memray results and their interpretation for
each stage. Curated stage files live in `reports/interpretations/`; source recordings
remain unchanged. The tab shows the **cold-start** CPU captures: three fresh
processes per workload, one measured request and no warm-up. Memory values come
from the original whole-test Memray capture; follow its separately stated scope.
The tab supports downloading the saved interpretation and printing / saving a PDF;
it never runs profilers or automatically generates findings.

The baseline tab uses these fixed report groups, recorded in
`reports/interpretations/baseline.json`:

| Displayed result | Pinned input | Calculation |
|---|---|---|
| Cold CPU phases | `profiles/cpu/cold-20261003T050725Z/summary.csv` and `medians.json` | Independent median of 3 accepted captures per workload/phase |
| Cold CPU functions | Session-1 `cpu.pstat` for each of the 4 workloads | Selected function calls, self CPU, inclusive CPU |
| Memory peak/breakdown | 4 original `pytest-memray` `.bin` recordings | High-watermark bytes ÷ 2²⁰; reviewed stack groups |

The interpretation also pins the readable session-1 CPU text reports. Every
source is checked by SHA-256 on dashboard refresh; a mismatch is visible and its
download link is disabled. Saved numbers are not silently recalculated from a
new report. The benchmark graph tab continues to discover `.benchmarks/`
independently.

`make benchmark` saves timing results in `.benchmarks/`. `make memprofile` saves
binary profiles in `profiles/memory/<UTC timestamp>-<random suffix>/` and prints
the directory plus its memory summary. Each run gets a new directory; generated
profiles are Git-ignored. The first baseline's existing memory profiles remain
in `measurements/baseline-bdf1239-gxSv1A8k/original-memory/`.
CPU profiles (`cpu.txt`, `cpu-self.txt` and `cpu.pstat`) are saved under
`profiles/cpu/<run>/session-N/<workload>/`, including when CPU is selected through
`make measure`. This directory is also Git-ignored.

Harness defaults: **3 independent sessions, 5 measured requests and 1 warm-up request**
per workload for non-CPU passes. **CPU profiling is cold by default:**
`make measure MEASURE_ARGS="--passes cpu"` captures one first request with zero
warm-ups in each fresh process (3 captures per workload). `--requests` and
`--warmup` do not change the CPU pass; use `--sessions` to change its repetition count.
Profiling starts before application imports and includes application/client startup;
fixture preparation, GET seeding, verification and shutdown are outside the profile.
PostgreSQL, MinIO and OS caches are not restarted or flushed. Keep new cold profiles
separate from the existing warm baseline profiles, which remain unchanged.
Choose and freeze the budget before collecting its baseline;
five samples do not establish a reliable tail percentile. Options include
`--requests`, `--sessions`, `--warmup`, `--workloads` and `--passes`.
`MEASURE_ARGS` affects **only `make measure`**, for example
`make measure MEASURE_ARGS="--stage baseline --passes diagnostic"`.

Non-CPU passes reset **only `postgres_test` and `runs-test`** through
`make test-setup`. Do not run other writers against these resources concurrently.
Each CPU capture instead creates its own `cpu_cold_<UUID>` database and
`cpu-cold-<UUID>` bucket, then removes those resources after verification.
Their names and cleanup status are recorded in `resources.json`.
POST verification reads **every persisted run** outside timing/CPU scope, so full
collections can take longer than the measured requests themselves.

Harness output: `measurements/<timestamp>/summary.csv`, plus per-session `result.json`,
worker logs and optional harness `memory.bin`. CPU profiles are stored separately
under `profiles/cpu/`; each CPU result records their paths. New CPU-time reports use
`session-N/<workload>/cpu_time/result.json`; older baseline reports keep the previous
`benchmark` folder/pass label and remain readable. Missing metrics remain empty/
`null`, not zero. Use only accepted **experiment** rows from the **cpu_time**
pass for supplementary harness latency/throughput graphs. CSV marks these rows with
`unprofiled_cpu_time` and reports observed timing overhead of instrumented passes
versus unprofiled runs; this includes noise. Cold CPU rows are marked
`profile_mode=cold` and excluded from that overhead calculation because cold
first requests and warmed workloads are not comparable.
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
| **Corrected baseline — no performance optimizations** | Original timing/memory workflows and supplementary CPU time/profiles/diagnostics for the four supplied scenarios | Establish the starting numbers after the separate correctness fix | Collected on `bdf1239`; primary numbers: `measurements/baseline-bdf1239-gxSv1A8k/primary-summary.csv`; supplementary numbers: `measurements/baseline-bdf1239-gxSv1A8k/supplementary/20261003T011935.704701Z/summary.csv` |
| **Connection pooling** | POST/GET latency, new connections and peak open connections | Fewer new connections and lower request latency; bounded open connections | Not measured |
| **One-pass encoding** | Original POST benchmark latency; cold profiled first-request CPU; original Memray peak | Less repeated work, lower memory use, and faster ingestion | Clean commit `8f5b534`: POST 500 median **2.107 → 0.570 s (−72.9%)**; POST 50 **0.620 → 0.360 s (−41.9%)**. Cold POST 500 first-request CPU **1.771 → 0.725 s (−59.0%)**. POST 500 peak **191.401 → 171.241 MiB (−10.5%)**; POST 50 **132.352 → 117.343 MiB (−11.3%)**. GET code unchanged. |
| **Reuse S3 SDK session, not client** | S3 client-setup CPU, original POST/GET latency and Memray peak; correctness across benchmark event loops | Shared session may cache SDK setup work. S3 clients still open/close per request, so S3 request and connection counts are not expected to fall. Memory improvement is uncertain. | First report: GET 10 KB median **89.8 → 41.1 ms**; GET 100 KB **99.9 → 48.1 ms**. POST 500 **594.8 → 566.7 ms**; POST 50 **394.4 → 322.9 ms**. Cold profiled `get_s3_client` CPU, session 1, POST 500: **0.1666 → 0.0323 s inclusive**. Diagnostic S3 requests/connections per request: POST **1 → 1**, GET **3 → 3**. See per-workload table and limitations below. |
| **Bulk database writes** | POST latency, ingestion throughput and database command count | Fewer database commands and faster batch ingestion | Not measured |
| **One S3 read per run** | GET latency, S3 request count and downloaded bytes | Fewer S3 requests and faster retrieval; measure the byte-transfer tradeoff | Not measured |

The rows highlight each optimization's main measurements. Use the three active
workflows (`make benchmark`, `make memprofile`, cold `make measure --passes cpu`)
at each stage. Keep other harness passes optional and separate. Record expected
effects before testing; do not invent expected percentages.

**Why session reuse first:** The one-pass cold CPU profile still shows about
0.162–0.167 s inclusive CPU in `get_s3_client` per first request. Original
Memray attributes 43.0 MiB of the POST-500 peak to **S3 SDK setup / data**.
This memory group includes data handling, so it is not a measurement of client
construction alone. Reusing one client for the whole application might avoid
more setup, but it also shares its HTTP connections and failure state across
requests, needs explicit startup/shutdown and recovery handling, and must work
with the supplied benchmark's new event loop per iteration. For this stage,
retain a fresh client per request and cache only the SDK session. Test whether
that actually helps; do not claim a memory or connection reduction in advance.

**First session-reuse results (one original benchmark report per stage):**

| Workload | Benchmark median, one-pass → session reuse | Cold first-request CPU median, one-pass → session reuse | Original Memray whole-test peak, one-pass → session reuse |
|---|---:|---:|---:|
| POST 500 × 10 KB | 594.8 → 566.7 ms | 0.725 → 0.577 s | 171.241 → 151.393 MiB |
| POST 50 × 100 KB | 394.4 → 322.9 ms | 0.547 → 0.414 s | 117.343 → 113.449 MiB |
| GET 10 KB | 89.8 → 41.1 ms | 0.346 → 0.219 s | 14.625 → 9.269 MiB |
| GET 100 KB | 99.9 → 48.1 ms | 0.467 → 0.339 s | 7.126 → 2.595 MiB |

| Diagnostic count per request (5 requests × 3 sessions) | Before | After |
|---|---:|---:|
| POST S3 requests / new S3 TCP connections | 1 / 1 | 1 / 1 |
| GET S3 requests / new S3 TCP connections | 3 / 3 | 3 / 3 |

The benchmark figures are provisional: each stage has one saved report, not
repeated independent benchmark reports or a confidence interval. Yappi CPU is
profiled CPU, not wall latency. `get_s3_client` still creates and closes a new
client on every request; the cached object is the SDK session only. Memray
captures each benchmark test separately. For later tests, the reused session
may have been allocated during an earlier test and fall outside that test's
capture; these peak changes are **not** full-process RSS savings. Check S3
connection counts separately rather than inferring them from timing or memory.
This candidate was uncommitted when measured; the optional measurement metadata
records its application fingerprint, but rerun the final comparison from a clean
revision before using these numbers as the presentation result.

Sources: one-pass `.benchmarks/Darwin-CPython-3.11-64bit/0004_baseline.json`;
session reuse `.benchmarks/Darwin-CPython-3.11-64bit/0005_baseline.json`;
session-reuse Memray `profiles/memory/20261003T203155Z-JBsZkyK5/` and cold
Yappi `measurements/20261003T203239.780065Z/`. Diagnostic comparison:
`measurements/baseline-bdf1239-gxSv1A8k/supplementary/20261003T011935.704701Z/`
and `measurements/20261003T203743.792132Z/`; both used five measured
requests per workload in each of three sessions and excluded startup/seeding.

One-pass reports: `.benchmarks/Darwin-CPython-3.11-64bit/0004_baseline.json`,
`profiles/memory/20261003T183206Z-4gQXLF1M/`, and
`measurements/20261003T183247.835556Z/summary.csv` with matching raw Yappi
files under `profiles/cpu/20261003T183247.835556Z/`. The dashboard labels the
benchmark report **One-pass encoder** and pins the CPU/memory sources in
`reports/interpretations/one-pass-encoder.json`. This is one benchmark report
per stage, not a confidence interval; do not attribute small GET timing shifts
to the POST change.

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
| S3 SDK session reuse | POST/GET latency; cold S3 setup CPU; peak allocated memory | Check whether client setup falls while clients stay request-scoped; report any retained-memory cost |
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
  harness, use `cpu_time`-pass timings for supplementary latency graphs, not profiler timings.
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
