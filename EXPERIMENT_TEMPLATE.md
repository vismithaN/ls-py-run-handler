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

## Logbook

Record dated discussion conclusions and decisions here, not only implementation
steps. Keep measured outcomes separate from hypotheses; proposals are not
approved or implemented changes.

### 2026-10-03 — optimization review and decisions

- **One-pass encoder — retained improvement.** Removed the repeated full-batch
  searches: **1,500 → 0** for POST 500. Cold first-request CPU fell
  **1.771 → 0.725 s**; whole-test tracked memory fell **191.4 → 171.2 MiB**.
  These are profiler measurements, not interchangeable with benchmark latency.

- **S3 session reuse — retained improvement.** Repeated on/off benchmarks
  supported lower GET latency: **87.8 → 41.1 ms** at 10 KB and
  **91.4 → 47.5 ms** at 100 KB (**53.2% / 48.0%** lower). POST was noisier.
  Clients and connections are still request-scoped; operation counts did not
  decrease. Lower Memray peaks do not prove equivalent full-process RAM savings.
  These repeats preceded the one-range GET; the benefit with that read path has
  not been separately isolated.

- **One S3 read per GET — request consolidation, not a demonstrated latency
  improvement.** The old **three reads ran concurrently**, not sequentially.
  Requests and observed new S3 connections fell **3 → 1**, but JSON decodes
  stayed **3 → 3**. Benchmark medians changed **41.1 → 39.3 ms** and
  **47.5 → 47.1 ms**, with overlapping ranges; memory was effectively unchanged.
  Keep the change as fewer S3 operations, without claiming a reliable latency
  or memory gain. No database migration was needed.

- **One JSON decode per GET / full-run DB reference — tested, then reverted.**
  The experiment added nullable `run_ref` through a migration. POST serialization
  calls fell **6,000 → 500 for the 500-run batch** and **600 → 50 for the
  50-run batch**; GET decodes fell **3 → 1**, while S3 reads stayed **1 → 1**.
  There was **no demonstrated latency gain**. POST 500 profiled CPU fell 6.2%,
  but its memory peak was effectively unchanged; other memory decreases were
  single-capture observations. Decision: these benefits did not justify schema
  migration, cross-version compatibility and deployment/rollback complexity.
  Reverted the code and migration; kept all reports. Removed its three benchmark
  repeats from the dashboard, preserving them in
  `measurements/whole-run-reference-validation/benchmark-reports/`.
  Submission wording: **implemented and tested, then rejected**, not never tried.

- **Next experiments — discussed only, not approved or implemented.** Use the
  retained version's CPU/memory profiles and diagnostics as inputs. First
  candidate: GET response serialization; other candidates: bulk database writes
  and application-lifetime S3 clients. Measure elapsed database/S3 phases too,
  because CPU profiles miss waiting. Change one thing at a time, verify
  correctness, and repeat unprofiled benchmarks before deciding to retain it.

- **GET response encoding — retained code change, not an optimization.**
  Decision recorded on **2026-10-03**: keep the direct `ORJSONResponse` return
  to reduce unnecessary response-conversion/encoding work on already
  JSON-compatible data. Do **not** count this as a potential or completed
  optimization in the experiment summary or submission. POST, S3 reads, database
  schema and connection lifetimes are unchanged. Preserve the tests, benchmark
  results and CPU/memory reports as validation history, not an optimization
  claim. The implementation remains **uncommitted**.

- **Bulk PostgreSQL writes — implemented and measured on 2026-10-03.**
  Replace 500/50 sequential INSERTs with one binary COPY inside a transaction.
  POST 500 median latency **540.9 → 321.3 ms (40.6% lower)**; database-write
  elapsed phase **226.6 → 12.6 ms**. Measured dispatches become **3**:
  BEGIN, COPY, COMMIT. POST 50 has a lower median but a worse mean; record it as
  mixed, not an across-the-board gain. Memory is unchanged. Recommendation:
  retain for the clear large-batch benefit; investigate smaller-batch tails
  before production latency claims. No schema, S3, GET, serialization or
  connection-lifetime change. Reports preserved; **not committed**.

- **POST 50 tail debugging — 2026-10-03.** Separately traced **200 requests**
  (100 per version), then **80 HTTP-detail requests**; all persisted contents
  verified. Database-write median **26.4 → 4.8 ms** in the first pass,
  **25.3 → 5.6 ms** in the follow-up. Large upload pauses occurred before and
  after COPY, with **zero S3 retries**. Example: a **771.7 ms** request spent
  **636.5 ms uploading**, versus **10.3 ms writing to PostgreSQL** and
  **0.3 ms in GC**. Connection setup was **1.3 ms**; local body writing took
  **235.2 ms**, followed by **380.5 ms** until final response headers.
  Conclusion: COPY saves database time; variable upload/network/server waiting
  can overwhelm the smaller batch's ~20 ms saving. Exact MinIO storage versus
  Docker/network cause remains unconfirmed. These are instrumented diagnostics,
  **not new benchmark points**. Original benchmarks/application code unchanged;
  raw reports, CSVs and interpretation saved under
  `measurements/bulk-post-tail-debug/20261004T041804Z-f9c992191ee6/` and
  `measurements/bulk-post-http-debug/20261004T042347Z-1d1d473ed0e4/`.

### 2026-10-04 — POST JSON parsing retention decision

**Tested, not retained.** Saved the tested experiment in `49fbd67`, then restored
the application and run tests from `f999bdc`. Keep the benchmark, CPU and memory
reports, source snapshots and dashboard interpretation as rejected experiment
history. The dashboard labels both the profile stage and benchmark point
**not retained**.

| Trade-off | Cost / justification |
|---|---|
| Keeping the change | Custom parsing, compatibility fallbacks, error translation and manual API-schema handling. POST 500 median was 7.2% higher; POST 50 was 7.3% lower; overall profiled CPU roughly unchanged. Mixed latency, not proof that all POSTs became slower. |
| Not retaining it | Give up **5.475 MiB / 3.743 MiB (3.6% / 3.4%)** of whole-test peak memory savings for POST 500 / POST 50, plus the POST 50 latency benefit observed in this repeat. These are not established production RAM or latency costs. |

Decision: the modest savings did not justify the extra request-validation
maintenance and mixed performance. Retain the simpler standard FastAPI path.

### 2026-10-04 — Concurrent POST memory check — incomplete

Single-request results for the payload-lifetime change were modest: whole-test
Memray savings **3.8% / 2.5%**, with mixed latency. Before accepting or rejecting
the checkpoint, test whether releasing Run models earlier helps when requests
overlap. This is a hypothesis, not an established production-capacity gain.

| Experiment | Target measurements | Expected outcome | Result |
|---|---|---|---|
| Before `b08cd4e` versus payload-lifetime checkpoint `7fe2538`; both original POST workloads; 1, 2, 4, 8 overlapping requests | Server RSS in MiB; successful requests/s; p95 latency; errors; observed server concurrency | Less server RAM at comparable throughput and acceptable latency, or no worthwhile benefit | Pilot passed; main comparison incomplete — no accepted result |

Separate real HTTP server and load-generator processes; one server worker,
fixed payloads, fresh server per trial, three warm-up requests and three repeats
per version/condition, alternating **B–A–A–B–B–A**. Use a dedicated temporary
database/bucket, not shared test resources. Keep original benchmarks, profiles,
application code and dashboard unchanged. Save raw samples and source hashes.

This first check uses fixed-concurrency clients: each sends its next request
after the previous finishes. Slower responses therefore reduce offered traffic;
lower RAM alone is not a capacity win. Check completed throughput and latency
together. Sample server RSS externally; do not multiply whole-test Memray peaks
by concurrency or include load-generator RAM. Stop safely if RAM/errors exceed
the experiment guardrails. No production SLO or enforced memory budget has been
chosen, so this does not establish maximum production capacity.

**Run outcome:** corrected two-second pilot **8/8 passed**, excluded from final
results. Before correctness **10/10** and after correctness/dashboard **15/15**
passed. Main run completed **14/48 trials**; trial 15 returned HTTP 500 and
logged S3 `PutObject: IncompleteBody`. Host free disk fell from about **1.7 GiB
to 65 MiB**. Timing was highly variable; disk pressure is an observed blocker,
not a proven cause of that upload error. **Do not claim a RAM/capacity improvement
from this incomplete run.** Dedicated database/bucket removed successfully;
partial reports and all source snapshots kept. No application change, revert,
dashboard update or commit made. Rerun needs at least **5 GiB free host disk**;
the harness now checks before starting and stops below **2 GiB**.
After cleanup, host free space recovered to about **3.2 GiB**, still below the
new 5 GiB preflight requirement.

Evidence: `measurements/payload-concurrency/20261004T184846Z-babd86acdb77/`.
Readable outcome: that folder's `FINDINGS.md`. The local, detachable harness is
`measurements/payload-concurrency/run.py`; original make targets are unchanged.

### 2026-10-04 — Concurrent POST memory check — fresh rerun completed

User freed disk space; preflight now reports about **27 GiB available**. Restart
the entire paired comparison, not only the unfinished cells. Same one-worker
server, fixed payloads, three warm-ups, eight-second dispatch window plus drain,
1/2/4/8 overlapping clients and **three repeats/version/condition** (48 trials).
The earlier incomplete run and all pilots are excluded. Disk/RSS guardrails
remain enabled. **48/48 trials completed, 1,423 measured POSTs, zero errors.**
Correctness before/after: **10/10 and 15/15 passed**. Every trial's expected
database row count and two exact GET round trips passed. Dedicated database and
bucket removed successfully; about **24 GiB** free disk remained afterward.

Values below are medians across three fresh-server trials per version/condition.
RSS is actual server RAM; requests/s means successful **POST batches/s**.

| POST workload | Concurrent clients | Peak RSS MiB, before → after | RAM change | Batches/s, before → after | p95 ms, before → after |
|---|---:|---|---:|---|---|
| 500 × 10 KB | 1 | 200.2 → 204.1 | +1.9% | 2.78 → 2.67 | 425 → 457 |
| 500 × 10 KB | 2 | 282.2 → 280.3 | −0.7% | 3.58 → 3.73 | 794 → 646 |
| 500 × 10 KB | 4 | 418.8 → 401.6 | −4.1% | 3.94 → 3.96 | 1,329 → 1,191 |
| 500 × 10 KB | 8 | 637.8 → 624.7 | −2.0% | 2.40 → 3.39 | 4,184 → 2,794 |
| 50 × 100 KB | 1 | 275.7 → 186.0 | −32.5% | 3.23 → 2.71 | 349 → 411 |
| 50 × 100 KB | 2 | 291.8 → 253.4 | −13.2% | 4.06 → 4.11 | 555 → 589 |
| 50 × 100 KB | 4 | 414.3 → 414.7 | +0.1% | 4.10 → 3.67 | 1,187 → 1,619 |
| 50 × 100 KB | 8 | 648.1 → 670.9 | +3.5% | 3.62 → 3.59 | 2,540 → 2,755 |

**Interpretation:** promising RAM benefit for POST 50 at two clients: all three
after peaks are below all three before peaks, with similar throughput. It is
**not a general capacity improvement**: at four clients RAM is unchanged but
throughput falls **10.4%** and p95 rises **36.3%**; at eight clients RAM is
higher. POST 500 never reaches the 10–15% RAM target. Its eight-client
throughput median improves 41.5%, but timing ranges overlap and old slow tails
remain unexplained; do not claim a consistent speedup. Single-client POST 50's
32.5% RAM reduction comes with 16.2% lower throughput, so is not a free win.

**Recommendation:** no broad production-optimization acceptance from this run.
Keep checkpoint unchanged for user review. A longer matched-arrival-rate test
with an explicit RAM/latency budget would be needed to justify the narrow
two-client benefit. No such follow-up, application edit, revert, new commit,
push or dashboard update was performed. Earlier incomplete run/pilots excluded.

Fresh reports: `measurements/payload-concurrency/20261004T191648Z-4084cd1cf8ea/`.

### 2026-10-04 — Final decision: POST payload lifetime — rejected and reverted

**User decision: keep this experiment out of the retained optimizations.** Restore
the application and related test assertions to `b08cd4e`; remove the helper,
destructive reverse/pop loop, early model-release assertions and eager COPY-row
materialization. Existing one-pass encoding, S3 session reuse, transactional COPY
and GET changes remain. No database migration or configuration change.

**Why:** whole-test memory savings were only 3.8% / 2.5%, with mixed latency.
Concurrent testing found a real narrow benefit (POST 50, two clients, **13.2%
lower server RSS** at similar throughput), but not a general capacity improvement.
At four clients that workload saved no RAM, while throughput fell **10.4%** and
p95 rose **36.3%**; at eight clients RAM increased. Accept giving up the narrow
benefit in exchange for the simpler previous code and avoiding this mixed trade-off.

**Verification after revert: 15/15 correctness/dashboard tests passed**, plus
10/10 on the frozen before snapshot. Application and run-test files match the
pre-experiment commit; Ruff checks/format and diff checks passed. Dedicated
verification database and bucket removed; shared test resources untouched.

**Kept:** checkpoint commit `7fe2538`, original benchmarks, CPU/Memray profiles,
all concurrency trials/source snapshots and interpretations as **tested, rejected**
history, not measurements of the restored code. No report deletion or dashboard
point added. **User approved committing this revert and logbook on 2026-10-04**;
no push was requested.
Verification evidence:
`measurements/payload-concurrency/20261004T193726Z-87bd3aa19092/`.

### 2026-10-04 — Simple POST copying and response experiment

**Retained as code cleanup, not counted as optimizations.** User decision on
**2026-10-04**: keep both changes and their correctness tests. Start from restored
`b2fe475`: (1) iterate
existing Run fields instead of `model_dump()`; (2) add direct POST
`ORJSONResponse`, explicitly preserving **201**. Standard validation, S3 batch
format/offsets, COPY, GET and connection lifetimes remain unchanged. No migration.

| Measurement | Baseline | Direct fields only | + Direct response |
|---|---:|---:|---:|
| POST 500 median latency, ms | 307.77 | 306.36 | 307.52 |
| POST 50 median latency, ms | 300.68 | 297.88 | 300.51 |
| POST 500 whole-test peak, MiB | 151.3847 | 151.3815 | 151.3789 |
| POST 50 whole-test peak, MiB | 109.4864 | 109.4617 | 109.4617 |
| POST 500 profiled request CPU, ms | 475.25 | 477.00 | 458.36 |
| POST 50 profiled request CPU, ms | 400.31 | 394.99 | 388.71 |

Three fresh suites/captures per version; rotated order. **15/15 correctness and
dashboard tests passed per version**, 9 benchmark suites, 36 Memray recordings
and 36 cold CPU captures. Dedicated resources cleaned; original make targets
and benchmark settings unchanged. Both application changes remain uncommitted.

**Interpretation:** combined median latency is effectively unchanged; memory
savings are **under 0.03%**, not the 10–15% target. Profiled CPU medians fall
3.56% / 2.90%, but ranges overlap and profiler overhead depends on call count.
`model_dump` calls fall **500/50 → 0**, but replacement iteration costs
11.34/1.15 ms versus 10.61/8.92 ms, so it is not a POST 500 CPU win. Direct response
removes **505/55 recursive conversion calls**. It is reasonable as a simple
processing cleanup, not a demonstrated latency/memory optimization. Keep the
reports as validation history; do not count either change in the optimization
summary or claim a latency/memory win in the submission. No dashboard point added.
GET 10 KB median rose 3.71% despite
unchanged GET code; do not hide that control result or attribute small movements
without further evidence.

Evidence and readable interpretation:
`measurements/simple-post-validation/20261004T195729Z-8eb8a0864a0e/FINDINGS.md`.
Full comparison, raw benchmark JSON, CPU/Memray recordings and all three frozen
source snapshots preserved locally; reports/scripts are Git-ignored.

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
| S3 session A/B repeat | Re-ran the original `make benchmark` suite three times with reuse on and three times off, in the order on–off–off–on–on–off. Each run reset the dedicated test database and bucket. The off version matched `8f5b534` in both application call sites; the on version matched `a765570`. The optimized application code was restored afterward. | **24/24 benchmark cases passed.** GET medians were consistently lower with reuse; POST was noisier. Raw reports are preserved in `measurements/session-reuse-validation/benchmark-reports/` (ignored by Git, outside the dashboard's stage series). |
| One S3 range per GET | Read the byte span covering all three saved fields from one object, then decode each field using its existing offsets. Keep the per-field path for references to different objects; leave POST and S3 client lifetime unchanged. Committed as `7b0961b`; retained after the full-run experiment. | **15/15 tests** (10 run, 5 dashboard), **12/12 benchmark cases**, **4/4 original Memray profiles**, **12/12 cold CPU captures** and **60/60 diagnostic requests** passed. GET S3 requests and new connections: **3 → 1 per request**. Benchmark latency changed little; see below. |
| One JSON decode per GET — not retained | Tested a full-run `run_ref`, one serialization per run, and one GET decode, with nullable-column migration `81b1d309a02a`. Experiment commit: `87d8666`. Reverted the application/migration changes; restored `7b0961b` storage behavior. | Original collection: **15/15 tests**, **12/12 benchmark cases**, **4/4 Memray profiles**, **12/12 cold CPU captures** passed. No demonstrated latency gain; modest profiled CPU/memory observations did not justify migration and rollout complexity. All reports and the dashboard experiment remain available. |
| GET response encoding — code change only | Keep the direct `ORJSONResponse` return to reduce response-conversion/encoding work. **Not a potential or completed optimization.** No POST, storage, schema or connection change. Uncommitted. | **15/15 tests**, **24/24 benchmark cases**, **8/8 original Memray profiles** and **24/24 cold CPU captures** passed. Preserve all measured results and reports as validation history; do not count them as a selected optimization. |
| Bulk PostgreSQL writes | Replace only the POST INSERT loop with a transactional binary COPY. IDs are already generated by the application; return them in request order after successful commit. No migration. Uncommitted. | **15/15 tests**, **24/24 original benchmark cases**, **8/8 original Memray profiles**, **24/24 accepted cold CPU captures**, and **120/120 diagnostic requests** passed. Duplicate-ID failures leave no partial database rows. Clear POST 500 latency gain; POST 50 is mixed. |

### GET response encoding — code-change validation

Purpose: reduce response-encoding work by removing the extra conversion walk.
**Retain the code change, but exclude it from potential/completed optimizations.**
The original hypothesis targeted GET CPU and latency; the measurements below
remain a factual record of that evaluation, not a selected optimization claim.
Fresh retained-version before/after suites ran **before–after–after–before–before–after**.
Latency below is the median of three report medians; CPU is the median of three
cold first-request profiles; memory is one original whole-test Memray peak per version.

| Workload | Median latency (ms), before → after | Profiled request CPU (s), before → after | Tracked peak (MiB), before → after | Interpretation |
|---|---:|---:|---:|---|
| POST 500 × 10 KB | 551.7 → 554.2 | 0.583 → 0.584 | 151.393 → 151.393 | Unchanged control; no POST code change. |
| POST 50 × 100 KB | 339.6 → 334.7 | 0.424 → 0.411 | 112.449 → 110.486 | Control variation; do not attribute this to GET encoding. |
| GET 10 KB | 40.9 → 41.0 | 0.196 → 0.179 | 9.240 → 9.220 | No clear latency/memory gain; profiled CPU 8.4% lower. |
| GET 100 KB | 46.6 → 41.7 | 0.314 → 0.181 | 2.595 → 2.595 | Latency 10.6% lower; profiled CPU 42.3% lower; memory unchanged. |

GET 100 KB report medians were **46.5–47.9 ms before** and **41.6–42.2 ms after**:
all three after reports were faster than all three before reports. The CPU
profiles confirm `jsonable_encoder` calls **6,157 → 0** (10 KB: **631 → 0**).
GET still uses **one S3 read and three storage JSON decodes**; HTTP output is
serialized once. Removing recursive calls also removes profiler overhead, so
the CPU percentages must not be applied to unprofiled latency or production CPU.
Memory peaks include the whole benchmark test, not server-only RAM or per-request
memory. These local serial benchmarks do not establish production p95 or concurrent capacity.

All raw reports, source snapshots, commands and cleanup evidence are under
`measurements/get-response-encoding-validation/20261004T025259Z-01c0342b29cc/`;
`comparison.json` contains the reproducible comparison. CPU recordings are in
`profiles/cpu/20261004T025553.290265Z/` (before) and
`profiles/cpu/20261004T025851.238611Z/` (after). Original Memray recordings are in
`profiles/memory/20261004T025259Z-get-response-{before,after}-01c0342b29cc/`.
The dashboard point is the **first completed after report**, not a selected best
run; the **10.6%** interpretation uses all six reports. The profile interpretation
pins **29 source files** in `reports/interpretations/get-response-encoding.json`.
Only isolated experiment databases/buckets were reset and removed; shared test
resources were not reset. Original benchmark code/settings and Makefile are unchanged.

### Bulk PostgreSQL writes — experiment result

Hypothesis: batching database writes reduces repeated exchanges and POST latency.
Only the INSERT loop changed; the before version includes the retained GET
encoding **code change**, which is not counted as an optimization. Six original
suites ran **before–after–after–before–before–after**. Median below means median
of three report medians; mean averages all saved round samples.

| Workload | Median latency (ms), before → after | Mean latency (ms), before → after | Interpretation |
|---|---:|---:|---|
| POST 500 × 10 KB | 540.9 → 321.3 | 550.8 → 325.7 | **40.6% lower median**; all after-suite medians are faster than all before-suite medians. |
| POST 50 × 100 KB | 342.1 → 309.7 | 348.0 → 369.7 | Median 9.5% lower, but mean 6.2% higher; **mixed**, not a uniform gain. |
| GET 10 KB | 40.5 → 38.4 | 40.2 → 40.4 | Unchanged code; overlapping ranges/control variation. |
| GET 100 KB | 42.2 → 41.7 | 42.2 → 42.0 | Unchanged code; no demonstrated GET improvement. |

POST 500 suite medians: **526.0–555.8 ms before**, **309.9–340.8 ms after**.
POST 50 ranges overlap; after samples include **550.5 ms and 1,011.0 ms**.
Their cause is not established. Keep them in the reports; do not claim improved
small-batch tails, production p95 or concurrent throughput from these local runs.

| Supporting measurement | POST 500, before → after | POST 50, before → after |
|---|---:|---:|
| Database-write elapsed phase (ms) | 226.6 → 12.6 | 23.6 → 4.8 |
| Measured database dispatches / POST | 500 → 3 | 50 → 3 |
| Cold profiled first-request CPU (s) | 0.596 → 0.469 (**21.4% lower**) | 0.409 → 0.401 (2.0% lower) |
| Whole-test Memray peak (MiB) | 151.393 → 151.393 | 110.486 → 110.486 |

The separate diagnostics used fixed paired fixtures, three sessions and five
measured requests per workload/version. Database phase excludes connection
setup, S3 upload and response encoding; it spans the INSERT loop or BEGIN through
COPY and COMMIT. The **3** dispatches are **BEGIN + COPY + COMMIT**, not one total
database command; driver metadata preparation is not a measured INSERT or wire
message. Each POST still makes **one S3 PUT, one new DB connection and one new
S3 connection**. Serialization remains **6,000/600 calls**. CPU percentages
include profiler overhead; memory is a whole-test allocation peak, not RSS.

Correctness covers generated/explicit IDs, response order, all stored references
and field contents, duplicate IDs within a batch and against existing rows,
rollback, and a successful write after failure. The ten named run tests remain
ten. Database failures still return the existing HTTP **500**. Rollback prevents
partial **database** rows, but does not remove an S3 object uploaded before the
failure; cross-store atomicity/cleanup is an unchanged limitation.

Raw reports, source snapshots, saved collector/analysis scripts and cleanup
records: `measurements/bulk-post-validation/20261004T033743Z-813debfb1f4a/`.
`comparison.json` holds the numeric comparison. CPU profiles:
`profiles/cpu/20261004T034029.839300Z/` (before),
`profiles/cpu/20261004T034320.733428Z/` (after). Memray:
`profiles/memory/20261004T033743Z-bulk-post-{before,after}-813debfb1f4a/`.
The dashboard adds the **first completed after report**, not the best run or the
three-report aggregate; its interpretation pins **35 source files** in
`reports/interpretations/bulk-postgres-writes.json`. Original benchmark source,
settings and Makefile are unchanged. Only unique experiment resources were reset
and removed; shared resources were not reset. **No commit made.**

The rejected experiment used `s3://bucket/object#start:end/run`, with an
exclusive end offset; its new rows stored `run_ref` instead of field references.
The retained implementation stores the original three field references and reads
their combined span with **one S3 GET and three JSON decodes**. The experimental
migration has been removed from the working tree. Validation used temporary
databases/buckets and removed only those resources; no shared database downgrade
or data deletion is part of this revert.

### Rejected experiment — production rollout cost

The full-run design would require a staged production rollout. Its experimental
reader supported both formats, but POST wrote only `run_ref`; old application
instances could not read those new rows. The requirements below explain the
complexity considered when rejecting it; they are **not the active deployment
plan**. Two application versions would coexist temporarily during rollout, not
permanently. Immediate backfill is optional while the legacy read path remains;
backfill becomes necessary to retire that format.

| Phase | Required behavior | Gate before continuing |
|---|---|---|
| Add the column | Add nullable `run_ref`; retain legacy columns. Use a short lock timeout and retry if PostgreSQL cannot acquire the schema-change lock. | Migration succeeds without prolonged blocking. |
| Deploy compatibility code | Temporarily write both whole-run and field references from the same S3 bytes. Keep readers for both formats; add a switch to disable whole-run reads during rollout. | Old readers retrieve new writes correctly; new readers retrieve old writes correctly. |
| Enable whole-run reads | Enable the new read path gradually while retaining both references. | Correct responses, acceptable latency/error rates, and one S3 request per new-format GET. |
| Backfill old rows | Process S3 batches once each, locate exact run byte boundaries, verify contents/identity, and update only missing `run_ref` values in small, resumable batches. Preserve legacy references. | Backfill progress and failures are visible; every promoted reference reads the expected run. |
| End dual writing | Stop writing field references only after every instance supports `run_ref` and the rollback window has ended. | Rollbacks now target a version that understands `run_ref`; rollback to the original reader is no longer safe for new rows. |
| Later cleanup | Remove legacy columns/fallback in a separate migration after all required rows have valid whole-run references. | No remaining reader, writer, or rollback version depends on legacy columns. |

Production acceptance tests must cover both cross-version read/write combinations,
rollback after new runs have been written, and interrupted/restarted backfill.
Temporary dual writing, rollout switches, and the backfill job were **not
implemented**. The experiment measured the final whole-run path; its benchmark
did not establish that a mixed-version deployment was safe.

One validation benchmark report is saved at
`measurements/run-range-validation-37e38105787c/benchmarks/Darwin-CPython-3.11-64bit/0001_run-range.json`.
Its medians are POST 500 **585.2 ms**, POST 50 **343.2 ms**, GET 10 KB **42.7 ms**,
and GET 100 KB **49.4 ms**. This single report verifies the workloads run; it does
not establish an additional latency or memory improvement.

### One JSON decode per GET — tested, then reverted

| Expected benefit | Measured outcome vs one-range GET | Decision |
|---|---|---|
| Fewer JSON calls should reduce CPU/latency; one reference should simplify reads. | GET decodes **3 → 1**, S3 reads **1 → 1**. No demonstrated latency gain. POST 500 profiled CPU **−6.2%**, memory effectively unchanged; other memory decreases came from single captures. | **Not retained:** no demonstrated benefit sufficient to justify a schema migration, cross-version compatibility, rollout/rollback checks, and eventual format retirement. |

Submission explanation: “I implemented and measured a full-run reference design,
but the results did not justify its production migration and deployment cost.
I reverted it and retained the simpler one-S3-read design, preserving the reports
as evidence of the decision.”

Revert verification: application code matches `7b0961b`; migration head is back
to `26a9efc758a0`. **15/15 tests passed** using an isolated database/bucket,
which were removed afterward. Both shared databases were checked read-only:
neither had `run_ref`, and neither was modified. **127 report/source files are
byte-for-byte unchanged**; the dashboard preserves the experiment as **not
retained**, with **15/15 pinned sources verified**. Revert changes are uncommitted.

Compare the previous **one-range, three-field-decode** implementation with
`87d8666`. Each cell is **previous → complete-run**. Latency is the median of
three independent benchmark-report medians; CPU is the median of three cold
first-request profiles. Memory is one original Memray whole-test peak per stage.

| Workload | Benchmark median (ms) | First-request CPU (s) | Peak tracked memory (MiB) |
|---|---:|---:|---:|
| POST 500 × 10 KB | 539.4 → 539.4 | 0.584 → 0.548 | 151.393 → 151.374 |
| POST 50 × 100 KB | 336.6 → 357.7 | 0.415 → 0.417 | 113.449 → 111.877 |
| GET 10 KB | 39.3 → 42.6 | 0.192 → 0.198 | 9.233 → 9.225 |
| GET 100 KB | 47.1 → 48.1 | 0.312 → 0.314 | 2.595 → 2.445 |

- **Latency:** no demonstrated gain. Observed changes are approximately 0.0%,
  +6.3%, +8.4%, and +2.1%, respectively. GET 10 KB report medians increased
  from 38.8–40.9 to 41.1–43.3 ms. These are historical before/after collections,
  not alternating A/B runs; do not attribute every difference to the code.
- **CPU:** POST 500 first-request CPU is 6.2% lower. Across all three profiles,
  POST serialization calls fall from 6,000 → 500 (POST 50: 600 → 50).
  POST 500 handler inclusive CPU falls from 0.130 → 0.092 s. GET decoding
  calls fall from 3 → 1, but still parse essentially the same payload;
  GET 100 KB decode CPU is only 0.383 → 0.369 ms. GET CPU does not meaningfully
  improve. Profiling CPU is not wall-clock benchmark latency.
- **Memory:** POST 50 is 1.4% lower and GET 100 KB is 5.8% lower in these
  captures; POST 500 and GET 10 KB are effectively unchanged. Single whole-test
  peaks are observations, not proof of a repeatable per-request memory saving.
- **Structural result:** one stored reference and one JSON decode for new rows.
  S3 GET count remains **1 → 1** versus the previous optimization; this change
  does not remove another network request.

Saved reports for `87d8666` (clean worktree during collection):

| Report | Location |
|---|---|
| Three original benchmark suites (archived outside the benchmark dashboard) | `measurements/whole-run-reference-validation/benchmark-reports/0007_whole-run-reference.json`, `0008_whole-run-reference.json`, `0009_whole-run-reference.json` |
| Four original Memray recordings | `profiles/memory/20261004T000943Z-whole-run-reference-f2f6dc2b3e42/` |
| Twelve cold Yappi profiles | `profiles/cpu/20261004T001316.404826Z/session-{1,2,3}/<workload>/` |
| CPU summary, samples and cleanup records | `measurements/20261004T001316.404826Z/summary.csv` and `session-{1,2,3}/<workload>/cpu/` |

The rejected experiment's three benchmark reports were moved outside `.benchmarks/`
without changing their contents, so the benchmark dashboard shows only the
baseline and retained optimizations. CPU/memory reports and their **not retained**
profile interpretation remain available.

The benchmark and memory commands used the original pytest workloads/settings
against a fresh, uniquely named real database/bucket, with resets between suites;
only the report label/output directory and isolated storage names changed.
CPU command: `make measure MEASURE_ARGS="--stage whole-run-reference --passes cpu"`.
CPU fixtures, harness/dependency fingerprints, Python and service configuration
match the preceding stage. All 12 captures are accepted with successful cleanup.
The preceding stage's pinned CPU/Memory sources are in
`reports/interpretations/one-s3-read.json`; its three benchmark paths are listed
in the one-range comparison below. Keep all raw reports; performance artifacts
are Git-ignored.

Session A/B report medians, in milliseconds. Each cell lists three independent
suite results; the percentage compares the median of those three values. These
are warm in-process API benchmarks, not cold CPU-profile timings.

| Workload | Session reuse off | Session reuse on | Median reduction |
|---|---:|---:|---:|
| POST 500 × 10 KB | 613.4, 590.7, 577.4 | 527.1, 554.2, 579.5 | 6.2% |
| POST 50 × 100 KB | 380.1, 341.5, 458.4 | 348.7, 335.8, 362.5 | 8.3% |
| GET 10 KB | 87.8, 86.0, 88.7 | 40.4, 42.8, 41.1 | 53.2% |
| GET 100 KB | 90.7, 91.4, 91.6 | 48.6, 47.0, 47.5 | 48.0% |

The clear repeatable result is for GET. POST direction is favorable, but three
suite runs are too few and too variable to claim a precise POST gain. The
uncached reports were collected with temporary source edits, so their embedded
Git metadata still names the checked-out `a765570` commit and marks it dirty;
the on/off assignment above is the experiment record. Files `0006`, `0009`,
`0010` are on; `0007`, `0008`, `0011` are off.

One-range GET comparison (three independent original benchmark reports per
stage; figures are the median of each report's median, in milliseconds):

| Workload | Session reuse | One S3 range | Change |
|---|---:|---:|---:|
| POST 500 × 10 KB | 554.2 | 539.4 | −2.7% |
| POST 50 × 100 KB | 348.7 | 336.6 | −3.5% |
| GET 10 KB | 41.1 | 39.3 | −4.4% |
| GET 100 KB | 47.5 | 47.1 | −0.9% |

The GET ranges overlap: 40.4–42.8 vs 38.8–40.9 ms for 10 KB; 47.0–48.6 vs
46.4–48.3 ms for 100 KB. Do **not** claim a reliable latency improvement from
these runs. The three one-range reports are
`.benchmarks/Darwin-CPython-3.11-64bit/0006_baseline.json`
and `measurements/one-s3-read-validation/benchmark-reports/0007_baseline.json`
and `0008_baseline.json`; the latter two are kept outside the dashboard stage
series. The session-reuse comparison reports are listed above.

Diagnostics (three sessions, five measured requests per workload): both GET
shapes changed from **3 S3 GETs / 3 new S3 connections** to **1 / 1** per
request. POST stayed **1 / 1**. GET downloaded **23 more bytes per request**
for the separators between fields: 34,410 → 34,433 bytes at 10 KB and
346,881 → 346,904 bytes at 100 KB. Cold profiled first-request CPU medians
were 0.219 → 0.192 s and 0.339 → 0.312 s for the two GETs; this Yappi scope is
not benchmark latency. Original Memray peaks were effectively unchanged
(GET 10 KB 9.269 → 9.233 MiB; GET 100 KB 2.595 → 2.595 MiB).
Sources: `measurements/20261003T222053.676199Z/` (diagnostic),
`measurements/20261003T221748.261877Z/` (cold CPU), and
`profiles/memory/20261003T221702Z-LfPxajIS/` (original Memray).

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
| **One S3 read per run** | GET latency, S3 request count and downloaded bytes | Fewer S3 requests; faster retrieval was a hypothesis; measure the byte-transfer tradeoff | **3 → 1 S3 requests and new connections per GET**, with +23 downloaded bytes. GET 10 KB median of three reports: **41.1 → 39.3 ms**; GET 100 KB: **47.5 → 47.1 ms**. Timing ranges overlap; latency improvement is not established. |
| **Spooled POST upload — not retained** | Original POST/GET latency, cold Yappi CPU and original Memray whole-test peak | Reduce the outgoing batch held in RAM; disk/file-body overhead may increase latency | POST 500: **151.4 → 143.6 MiB (−5.1%)**, median **324.6 → 362.4 ms (+11.7%)**. POST 50: **111.5 → 105.6 MiB (−5.3%)**, median **322.7 → 350.5 ms (+8.6%)**. GET latency and memory essentially unchanged. **Rejected:** the modest memory saving did not justify the POST 500 latency cost. |
| **Direct POST JSON parsing — tested, not retained** | Original POST latency and whole-test Memray peak; cold Yappi CPU; validation compatibility | Parse raw bytes directly into validated Run models, removing the intermediate decoded request tree. Lower memory/CPU is a hypothesis; no saving percentage promised. | POST 500: **151.4 → 145.9 MiB (−3.6%)**, median **370.9 → 397.7 ms (+7.2%)**. POST 50: **109.5 → 105.8 MiB (−3.4%)**, median **346.1 → 320.9 ms (−7.3%)**. Overall profiled POST CPU is roughly unchanged. **Rejected:** modest memory savings did not justify mixed latency and custom validation maintenance. Reports retained; code reverted. |

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

### Spooled-upload experiment

Change only POST's outgoing buffer: `SpooledTemporaryFile` spills batches above
1 MiB to disk; assemble one run at a time, close its buffer immediately, rewind
the spool and pass it directly to S3 with `ContentLength`. Storage bytes and
offsets, GET, database COPY and client lifetimes stay unchanged. Merely moving
`getvalue()` inline would not reduce memory: a variable is a reference, not a
second copy of the payload.

Fresh before/after order: **B–A–A–B–B–A**. Each version has three original
benchmark suites, three original Memray suites and three cold CPU captures per
workload. Original workloads/options were not edited. The first tiny-write
prototype was slower; per-run chunks reduce spool writes from **10,001 to 502**.
Preserve its reports separately, not in the final averages.

Cold first-request CPU: POST 500 **0.473 → 0.493 s (+4.3%)**; POST 50
**0.421 → 0.412 s (−2.1%)**. No consistent CPU improvement. POST 50 latency
is mixed: median worsens but mean **355.8 → 352.2 ms** and suite ranges overlap.

Results and exact source snapshots:
`measurements/spooled-post-validation/20261004T051112Z-e9892cf27e31/`.
Dashboard interpretation: `reports/interpretations/spooled-post-buffer.json`.
The benchmark graph adds the first completed final after report; the interpretation
uses all three reports per version. Memory is whole-test tracked allocation,
not RSS. The experimental peak shifted to request parsing. **Decision: not retained;
the ~5% memory saving did not justify the POST 500 slowdown and disk dependence.**
**Completed and reverted:** saved the tested implementation in `0def488`, then
reverted the spooled-upload code and its related tests in `eb08946`, restoring
the previous in-memory POST upload. The ~5% memory reduction was not worth the
consistent 11.7% POST 500 latency increase and added disk dependence.
**Kept:** benchmark, CPU and memory reports, source snapshots, and dashboard
interpretation, labelled **not retained**. These are historical experiment
results, not measurements of the restored code. **Validation after revert:
15/15 tests passed.** No push made.

### Direct POST JSON parsing — tested, not retained

The tested candidate used `TypeAdapter(List[Run]).validate_json(body)`:
raw bytes → validated models, without a separate decoded Python payload tree.
The old decoder was a fallback for uncommon encodings and deeply nested JSON.
HTTP validation statuses and field locations were preserved; some error types/
wording and the inline OpenAPI schema representation differed. Upload buffering,
storage bytes, GET, database COPY and S3 lifetimes were unchanged.

Repeated **B–A–A–B–B–A**: three original benchmark suites and three original
Memray suites per version; three cold CPU captures per workload/version.
Memory savings are modest and repeatable. POST 500 parsing + model attribution
is **31.3 → 28.9 MiB**, not a saving of the whole former 22.7 MiB JSON group.
The validated data still needs RAM; groups describe stacks at the recorded peak.

Latency is **mixed**. POST 500 suite medians overlap; POST 50 after-suite medians
are all lower in this repeat. Means: **391.4 → 419.1 ms** and
**408.4 → 326.4 ms**, respectively. Cold request CPU:
**0.475 → 0.466 s** and **0.399 → 0.404 s**—no consistent overall CPU gain.
GET code is unchanged; its Memray capture includes POST seeding.
**15 tests passed**; still ten run test functions. Temporary experiment resources
were cleaned; shared test resources were not reset.

Final reports/source snapshots:
`measurements/post-json-validation/20261004T063926Z-1898efc8965d/`.
Dashboard: `reports/interpretations/post-json-parsing.json`, with 56 pinned sources.
The benchmark graph uses the first completed after report
(`0010_post-json-parsing.json`); the interpretation uses all three per version.
Earlier captures are preserved separately but excluded: they preceded nesting
or validation-error compatibility fixes. Memory is whole-test tracked allocation,
not server RSS. **Decision: tested, not retained.** Saved the experiment in
`49fbd67`; reverted its implementation and related run-test changes, restoring
`f999bdc`'s standard FastAPI request parsing. Keep all reports and the numeric
grouping dashboard improvements. The rejection accepts the lost 3.6% / 3.4%
whole-test memory savings in exchange for simpler validation; mixed latency and
roughly unchanged overall CPU did not justify retaining the custom parser.
**Validation after revert: 15/15 tests passed** (ten run tests and five dashboard
tests), with the two existing FastAPI deprecation warnings. Verified the restored
application fingerprint matches the fresh before version. Temporary verification
database/bucket were removed; shared test resources and all raw reports were
preserved. No new benchmark/profile point or push was made for the revert.

### 2026-10-04 — POST payload lifetime experiment checkpoint

Hypothesis: release each validated Run after encoding it, retaining only compact
COPY rows and response IDs, to reduce overlap with the growing S3 batch.
Compare against retained `b08cd4e` code, **not** the rejected direct-JSON parser.

| Measurement | POST 500 × 10 KB | POST 50 × 100 KB |
|---|---|---|
| Whole-test Memray peak | **151.4 → 145.6 MiB (−3.8%)** | **112.4 → 109.6 MiB (−2.5%)** |
| Original benchmark median | **366.0 → 336.8 ms (−8.0%)** | **337.8 → 364.4 ms (+7.9%)** |
| Pooled benchmark mean | **364.5 → 355.5 ms (−2.5%)** | **343.0 → 440.7 ms (+28.5%)** |
| Cold first-request CPU | **0.4811 → 0.4814 s (+0.1%)** | **0.4129 → 0.4008 s (−2.9%)** |

Original four workloads/settings; three unprofiled benchmark suites and three
pytest-memray suites per version in **B–A–A–B–B–A** order; three cold Yappi
captures per workload/version. **15/15 correctness/dashboard tests passed**, with
weak-reference assertions proving earlier model release; ten run test functions
remain. **24 benchmark cases, 24 original memory recordings and 24 cold CPU
captures** completed. Serialization remains 6,000/600 POST calls and one COPY.
GET implementation, validation, S3 bytes/offsets and database schema unchanged.

**The 10–15% memory target was not met.** Cached decoded request data, HTTP body
and outgoing batch remain; allocator-stack groups are not exact live-object sizes.
POST latency is mixed; POST 50 is slower in all three after-suite medians, and its
slow samples are retained. Do not claim a uniform latency or production RAM gain.
The candidate is **checkpointed at the user's request for review**, not accepted
as a selected optimization. No push or dashboard stage was added in this side
experiment at that point. **Later decision on 2026-10-04: user rejected it and
requested a revert; application/tests restored to `b08cd4e`, 15/15 tests passed.**
Keep these measurements as historical evidence of the rejected candidate.

Reports and frozen source snapshots:
`measurements/payload-lifetime-validation/20261004T073555Z-f8b6dbe95489/`.
Final test-wrapper recheck:
`measurements/payload-lifetime-validation/20261004T074639Z-29976d1a9b28/`.
All experiment/CPU databases and buckets were isolated and removed; shared test
resources were not reset. Raw memory and CPU reports remain under `profiles/`.

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
