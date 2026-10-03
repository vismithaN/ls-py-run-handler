"""Optional harness: separate CPU-time, CPU-profile, memory and diagnostic passes.

Usage: make measure MEASURE_ARGS="--stage baseline"
Use --passes cpu_time for unprofiled CPU seconds and supplementary request timing.
Use --passes cpu for cold imports/startup plus one request in a fresh process.
CPU profiles are saved under profiles/cpu/<run>/session-N/<workload>/.
The supplied make benchmark and make memprofile commands do not use this harness.
Keep harness timing/memory results separate from the supplied benchmark results.
Non-CPU passes reset ONLY postgres_test/runs-test through make test-setup.
Do not run other writers against those resources at the same time.
CPU captures use their own temporary database/bucket and clean them up afterward.
"""

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from scripts.measurement_workloads import WORKLOADS, expected_run, fixture

ROOT = Path(__file__).resolve().parents[1]
PASSES = ("cpu_time", "cpu", "memory", "diagnostic")


def require_test_environment():
    from ls_py_handler.config.settings import settings

    local = {"localhost", "127.0.0.1", "::1"}
    if (
        os.environ.get("RUN_HANDLER_ENV") != "test"
        or settings.DB_NAME != "postgres_test"
        or settings.S3_BUCKET_NAME != "runs-test"
        or settings.DB_HOST not in local
        or urlparse(settings.S3_ENDPOINT_URL).hostname not in local
    ):
        raise RuntimeError(
            "Harness requires test environment and local postgres_test/runs-test"
        )
    return settings


def command_output(args):
    try:
        return subprocess.check_output(
            args, cwd=ROOT, text=True, stderr=subprocess.DEVNULL, timeout=10
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def fingerprint(paths):
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def environment(settings):
    versions = {}
    for package in (
        "fastapi",
        "asyncpg",
        "aiobotocore",
        "aiohttp",
        "httpx",
        "orjson",
        "yappi",
        "memray",
        "pytest-benchmark",
    ):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    docker_info = command_output(["docker", "info", "--format", "{{json .}}"])
    docker = json.loads(docker_info) if docker_info else {}
    images = command_output(
        [
            "docker-compose",
            "-f",
            "docker-compose-db.yaml",
            "--profile",
            "postgres-15",
            "images",
            "--format",
            "json",
        ]
    )
    images = json.loads(images) if images else None
    limits = {}
    for image in images or []:
        name = image["ContainerName"]
        info = command_output(
            [
                "docker",
                "inspect",
                name,
                "--format",
                '{"memory_bytes":{{.HostConfig.Memory}},'
                '"nanocpus":{{.HostConfig.NanoCpus}}}',
            ]
        )
        limits[name] = json.loads(info) if info else None
    ram = (
        command_output(["sysctl", "-n", "hw.memsize"])
        if sys.platform == "darwin"
        else None
    )
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "logical_cpus": os.cpu_count(),
        "host_ram_bytes": int(ram) if ram else None,
        "dependencies": versions,
        "docker_version": docker.get("ServerVersion"),
        "docker_cpus": docker.get("NCPU"),
        "docker_memory_bytes": docker.get("MemTotal"),
        "service_images": images,
        "container_limits": limits,
        "git_revision": command_output(["git", "rev-parse", "HEAD"]),
        "dirty_worktree": bool(command_output(["git", "status", "--porcelain"])),
        "application_sha256": fingerprint((ROOT / "ls_py_handler").rglob("*.py")),
        "migration_sha256": fingerprint((ROOT / "migrations").rglob("*.py")),
        "harness_sha256": fingerprint(
            [
                *(ROOT / "scripts").glob("measurement*.py"),
                Path(__file__),
            ]
        ),
        "lock_sha256": hashlib.sha256((ROOT / "poetry.lock").read_bytes()).hexdigest(),
        "database": {
            "host": settings.DB_HOST,
            "port": settings.DB_PORT,
            "name": settings.DB_NAME,
        },
        "object_store": {
            "endpoint": settings.S3_ENDPOINT_URL,
            "bucket": settings.S3_BUCKET_NAME,
        },
    }


def summarize(samples, elapsed_s, batch_size, method):
    correct = [sample for sample in samples if sample["outcome"] == "correct"]
    durations = sorted(sample["duration_ns"] / 1e6 for sample in correct)
    count = len(correct)
    return {
        "attempted": len(samples),
        "correct": count,
        "failures": sum(sample["outcome"] == "failure" for sample in samples),
        "timeouts": sum(sample["outcome"] == "timeout" for sample in samples),
        "median_ms": statistics.median(durations) if count else None,
        "mean_ms": statistics.mean(durations) if count else None,
        "p95_ms": durations[math.ceil(0.95 * count) - 1] if count else None,
        "p95_small_sample": count < 20,
        "mean_request_body_bytes": statistics.mean(
            sample["request_body_bytes"] for sample in correct
        )
        if count
        else None,
        "mean_response_body_bytes": statistics.mean(
            sample["response_body_bytes"] for sample in correct
        )
        if count
        else None,
        "elapsed_workload_s": elapsed_s,
        "elapsed_request_s": sum(sample["duration_ns"] for sample in samples) / 1e9,
        "requests_per_s": count / elapsed_s if elapsed_s else None,
        "runs_per_s": count * batch_size / elapsed_s
        if method == "POST" and elapsed_s
        else None,
    }


async def call(client, data, target, timeout):
    async with asyncio.timeout(timeout):
        if data["method"] == "GET":
            return await client.get(f"/runs/{target}")
        return await client.post(
            "/runs", content=data["body"], headers={"Content-Type": "application/json"}
        )


async def validate(client, response, data, target, timeout):
    """Validate ALL fields and ALL persisted POST runs, outside measurements."""
    if response is None:
        return False, "Request did not return a response"
    if response.status_code != (201 if data["method"] == "POST" else 200):
        return False, f"Unexpected HTTP {response.status_code}"
    try:
        value = response.json()
        if data["method"] == "GET":
            if value != expected_run(data["runs"][0], target):
                return False, "Retrieved run differs from submitted run"
        else:
            ids = value["run_ids"]
            if (
                value.get("status") != "created"
                or len(ids) != data["batch_size"]
                or len(set(ids)) != len(ids)
            ):
                return False, "Invalid POST acknowledgement"
            for run_id, run in zip(ids, data["runs"], strict=True):
                async with asyncio.timeout(timeout):
                    stored = await client.get(f"/runs/{run_id}")
                if stored.status_code != 200 or stored.json() != expected_run(
                    run, run_id
                ):
                    return False, "Persisted run differs from submitted run"
        return True, None
    except Exception as error:
        return False, f"Verification failed: {type(error).__name__}"


async def measure_session(args, folder):
    settings = require_test_environment()
    metadata = environment(settings)
    if args.passes[0] == "cpu":
        from scripts.measurement_cpu import collect

        return await collect(args, folder, settings, metadata)

    import asyncpg

    from ls_py_handler.main import app

    connection = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
    )
    try:
        metadata["database"]["configuration"] = dict(
            await connection.fetchrow(
                "SELECT current_setting('server_version') AS version, "
                "current_setting('max_connections') AS max_connections, "
                "current_setting('shared_buffers') AS shared_buffers"
            )
        )
    finally:
        await connection.close()

    if args.passes[0] != "memory":
        return await workload_session(args, folder, app, settings, metadata)

    import memray

    # The agreed memory scope is the entire workload session: fixture/client
    # creation, startup, seed, warmup, measured requests, verification, shutdown.
    # Metadata collection above is outside this scope, as are module imports.
    with memray.Tracker(
        folder / "memory.bin",
        trace_python_allocators=True,
        file_format=memray.FileFormat.AGGREGATED_ALLOCATIONS,
    ):
        result = await workload_session(args, folder, app, settings, metadata)
    reader = memray.FileReader(folder / "memory.bin")
    peak = sum(record.size for record in reader.get_high_watermark_allocation_records())
    result["profile"] = {
        "peak_allocated_bytes": peak,
        "peak_allocated_mib": peak / 1024**2,
        "artifact": "memory.bin",
        "format": "aggregated_allocations",
        "trace_python_allocators": True,
        "native_traces": False,
        "scope": (
            "whole workload session including fixtures/client/startup/seed/"
            "warmup/verification/shutdown; metadata/imports excluded; not RSS"
        ),
    }
    result["summary"]["peak_allocated_mib"] = peak / 1024**2
    return result


async def workload_session(args, folder, app, settings, metadata):
    data = fixture(args.workloads[0], args.seed, args.smoke)
    collector = None
    if args.passes[0] == "diagnostic":
        from scripts.measurement_collectors import Diagnostics

        collector = Diagnostics(settings.DB_PORT, settings.S3_ENDPOINT_URL)

    def phase(name):
        if collector:
            collector.begin(name)

    samples, responses = [], []
    seen_ids = set()
    profile = {}
    method = data["method"]
    target = None
    # Each worker has exactly one event loop, lifecycle and client. Hooks remain
    # active through startup/warmup so retained pooled connections are visible.
    with collector if collector else nullcontext():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                phase("seed")
                if method == "GET":
                    post_data = {**data, "method": "POST"}
                    seeded = await call(client, post_data, None, args.timeout)
                    ok, reason = await validate(
                        client, seeded, post_data, None, args.timeout
                    )
                    if not ok:
                        raise RuntimeError(f"GET seed failed: {reason}")
                    target = seeded.json()["run_ids"][0]
                warm_responses = []
                phase("warmup")
                for _ in range(args.warmup):
                    warm_responses.append(
                        await call(client, data, target, args.timeout)
                    )
                phase("warmup_verification")
                for response in warm_responses:
                    ok, reason = await validate(
                        client, response, data, target, args.timeout
                    )
                    if not ok:
                        raise RuntimeError(f"Warm-up failed: {reason}")
                    if method == "POST":
                        seen_ids.update(response.json()["run_ids"])
                del warm_responses

                phase("measured")
                cpu_before = time.process_time_ns()
                try:
                    workload_started = time.perf_counter_ns()
                    for index in range(args.requests):
                        before = collector.snapshot() if collector else None
                        response, error, outcome = None, None, "pending"
                        started = time.perf_counter_ns()
                        try:
                            response = await call(client, data, target, args.timeout)
                        except (TimeoutError, httpx.TimeoutException):
                            outcome, error = "timeout", "Request timed out"
                        except Exception as exc:
                            outcome, error = "failure", type(exc).__name__
                        duration = time.perf_counter_ns() - started
                        sample = {
                            "request": index + 1,
                            "duration_ns": duration,
                            "status": response.status_code
                            if response is not None
                            else None,
                            "request_body_bytes": len(data["body"])
                            if method == "POST"
                            else 0,
                            "response_body_bytes": len(response.content)
                            if response is not None
                            else None,
                            "outcome": outcome,
                            "error": error,
                        }
                        if collector:
                            after = collector.snapshot()
                            sample["diagnostic"] = {
                                "counts": {
                                    key: value - before[0].get(key, 0)
                                    for key, value in after[0].items()
                                    if not key.endswith(("peak_open", "open_at_start"))
                                },
                                "operations": {
                                    key: value - before[1].get(key, 0)
                                    for key, value in after[1].items()
                                },
                            }
                        samples.append(sample)
                        responses.append(response)
                    workload_ns = time.perf_counter_ns() - workload_started
                finally:
                    cpu_ns = time.process_time_ns() - cpu_before
                phase("verification")
                for sample, response in zip(samples, responses, strict=True):
                    if sample["outcome"] != "pending":
                        continue
                    ok, reason = await validate(
                        client, response, data, target, args.timeout
                    )
                    if ok and method == "POST":
                        sample["run_ids"] = response.json()["run_ids"]
                        ids = set(sample["run_ids"])
                        if ids & seen_ids:
                            ok, reason = False, "POST reused IDs from another request"
                        seen_ids.update(ids)
                    sample["outcome"] = "correct" if ok else "failure"
                    sample["error"] = reason
                phase("shutdown")
    elapsed = workload_ns / 1e9
    summary = summarize(samples, elapsed, data["batch_size"], method)
    # Clock reads are cheap; use unprofiled process CPU for the CPU-time graph.
    # Yappi's instrumented CPU total remains in the separate profile object.
    summary["cpu_s"] = cpu_ns / 1e9 if args.passes[0] == "cpu_time" else None
    summary["cpu_s_per_request"] = (
        summary["cpu_s"] / args.requests if summary["cpu_s"] is not None else None
    )
    summary["peak_allocated_mib"] = profile.get("peak_allocated_mib")
    diagnostic = collector.result() if collector else None
    if diagnostic:
        counts = diagnostic["phases"]["measured"]["counts"]
        for key, value in counts.items():
            summary[key] = value
        if counts["stored_bytes_unknown_objects"]:
            summary["s3_stored_object_bytes"] = None
        if counts["upload_bytes_unknown_attempts"]:
            summary["s3_attempted_upload_body_bytes"] = None
        summary["postgres_commands_per_request"] = (
            counts["postgres_commands"] / args.requests
        )
        summary["s3_attempts_per_request"] = counts["s3_attempts"] / args.requests
    return {
        "schema_version": 1,
        "stage": args.stage,
        "purpose": "harness-validation" if args.smoke else "experiment",
        "workload": ("smoke-" if args.smoke else "") + args.workloads[0],
        "pass": args.passes[0],
        "session": args.session,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "accepted": summary["correct"] == args.requests,
        "procedure": {
            "requests": args.requests,
            "warmup": args.warmup,
            "sessions": args.sessions,
            "seed": args.seed,
            "concurrency": 1,
            "timeout_s": args.timeout,
            "transport": "in-process ASGI",
            "lifecycle": "one loop/app/client per worker",
            "cold_or_warm": (
                "warm" if args.warmup else "no warmup; GET seed may warm resources"
            ),
            "timing_scope": (
                "complete API call latency; sequential workload elapsed for rate; "
                "setup/verification excluded"
            ),
            "server_ids": "generated; returned IDs used for verification",
            "get_target": target,
            "batch_size": data["batch_size"],
            "field_size_kb": data["field_size_kb"],
            "fixture_sha256": data["sha256"],
        },
        "environment": metadata,
        "summary": summary,
        "samples": samples,
        "profile": profile or None,
        "diagnostic": diagnostic,
    }


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def cpu_profile_directory(folder):
    """Map a worker's run/session/workload/cpu folder to profiles/cpu."""
    run = folder.parents[2]
    destination = ROOT / "profiles" / "cpu" / run.name / folder.relative_to(run).parent
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def write_summary(folder):
    rows = []
    for path in sorted(folder.glob("session-*/*/*/result.json")):
        result = json.loads(path.read_text())
        rows.append(
            {
                key: result[key]
                for key in (
                    "stage",
                    "purpose",
                    "workload",
                    "pass",
                    "session",
                    "accepted",
                )
            }
            | result["summary"]
            | {
                "profile_mode": (result.get("profile") or {}).get("mode"),
                "result_file": str(path.relative_to(folder)),
            }
        )
    if rows:
        # "benchmark" is the old label in saved baseline reports; leave them intact.
        for row in rows:
            row["unprofiled_cpu_time"] = row["pass"] in ("cpu_time", "benchmark")
        references = {
            (row["workload"], row["session"]): row
            for row in rows
            if row["unprofiled_cpu_time"]
        }
        for row in rows:
            reference = references.get((row["workload"], row["session"]))
            # An observed pass-to-pass difference, not a noise-free causal
            # estimate of profiler overhead. Never use these timings as wins.
            row["observed_timing_overhead_pct"] = None
            if (
                not row["unprofiled_cpu_time"]
                # Cold first requests are not comparable with warmed references.
                and row["profile_mode"] != "cold"
                and row["accepted"]
                and reference
                and reference["accepted"]
                and reference["mean_ms"]
                and row["mean_ms"] is not None
            ):
                row["observed_timing_overhead_pct"] = (
                    row["mean_ms"] / reference["mean_ms"] - 1
                ) * 100
        columns = list(dict.fromkeys(key for row in rows for key in row))
        with (folder / "summary.csv").open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    return rows


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--stage", default="baseline")
    parser.add_argument("--passes", choices=PASSES, nargs="+", default=list(PASSES))
    parser.add_argument(
        "--workloads", choices=WORKLOADS, nargs="+", default=list(WORKLOADS)
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=5,
        help="Measured requests for non-CPU passes; cold CPU always uses one",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Warm-up requests for non-CPU passes; cold CPU always uses zero",
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=3,
        help="Independent sessions per workload/pass (fresh starts for CPU)",
    )
    parser.add_argument("--seed", type=int, default=20260804)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--output", type=Path, default=ROOT / "measurements")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Tiny 2-run/1-KB fixtures; NOT baseline evidence",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--session", type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.requests < 1 or args.sessions < 1 or args.warmup < 0 or args.timeout <= 0:
        parser.error(
            "requests/sessions/timeout must be positive; warmup must be nonnegative"
        )
    require_test_environment()
    if args.passes == ["cpu"]:
        args.requests, args.warmup = 1, 0
    if args.worker:
        if len(args.passes) != 1 or len(args.workloads) != 1:
            parser.error("A worker runs one pass and one workload")
        result = asyncio.run(measure_session(args, args.output))
        write_json(args.output / "result.json", result)
        return 0 if result["accepted"] else 1

    # New directories never overwrite a previous experiment or profile.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    folder = args.output / stamp
    folder.mkdir(parents=True, exist_ok=False)
    write_json(
        folder / "experiment.json",
        {
            "stage": args.stage,
            "passes": args.passes,
            "workloads": args.workloads,
            "requests": args.requests,
            "warmup": args.warmup,
            "sessions": args.sessions,
            "seed": args.seed,
            "timeout_s": args.timeout,
            "smoke": args.smoke,
            "pass_options": {
                name: {
                    "requests": 1 if name == "cpu" else args.requests,
                    "warmup": 0 if name == "cpu" else args.warmup,
                    **({"profile_mode": "cold"} if name == "cpu" else {}),
                }
                for name in args.passes
            },
        },
    )
    if "cpu" in args.passes:
        print(
            "CPU: cold application imports/startup + one request per fresh process; "
            "no warm-up. Services and OS caches stay running.",
            flush=True,
        )
    failed = False
    for session in range(1, args.sessions + 1):
        for workload in args.workloads:
            for collection in args.passes:
                target = folder / f"session-{session}" / workload / collection
                target.mkdir(parents=True)
                cold_cpu = collection == "cpu"
                requests, warmup = (1, 0) if cold_cpu else (args.requests, args.warmup)
                setup = "isolated cold CPU capture" if cold_cpu else "clean test setup"
                print(
                    f"Session {session}: {workload} / {collection} — {setup}",
                    flush=True,
                )
                if not cold_cpu:
                    subprocess.run(["make", "test-setup"], cwd=ROOT, check=True)
                cmd = [
                    sys.executable,
                    "-m",
                    "scripts.measure",
                    "--worker",
                    "--stage",
                    args.stage,
                    "--passes",
                    collection,
                    "--workloads",
                    workload,
                    "--requests",
                    str(requests),
                    "--warmup",
                    str(warmup),
                    "--sessions",
                    str(args.sessions),
                    "--session",
                    str(session),
                    "--seed",
                    str(args.seed),
                    "--timeout",
                    str(args.timeout),
                    "--output",
                    str(target),
                ]
                if args.smoke:
                    cmd.append("--smoke")
                with (target / "worker.log").open("w") as output:
                    completed = subprocess.run(
                        cmd, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT
                    )
                failed |= completed.returncode != 0
                path = target / "result.json"
                if path.exists():
                    result = json.loads(path.read_text())
                    print(
                        f"  {result['summary']['correct']}/{requests} correct; "
                        f"result: {path}",
                        flush=True,
                    )
                else:
                    write_json(
                        target / "error.json",
                        {"worker_exit_code": completed.returncode, "log": "worker.log"},
                    )
                    print(
                        f"  Worker failed; inspect {target / 'worker.log'}", flush=True
                    )
                write_summary(folder)
    print(f"Results: {folder / 'summary.csv'}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
