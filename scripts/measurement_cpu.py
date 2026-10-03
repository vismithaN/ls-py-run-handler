"""Cold CPU capture. Keep application imports inside the profiled worker."""

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


@asynccontextmanager
async def isolated_resources(settings, folder, metadata):
    """Use and remove only resources created for this individual capture."""
    import asyncpg
    from aiobotocore.session import get_session

    suffix = uuid.uuid4().hex
    database, bucket = f"cpu_cold_{suffix}", f"cpu-cold-{suffix}"
    original_database, original_bucket = settings.DB_NAME, settings.S3_BUCKET_NAME
    record = {
        "database": database,
        "bucket": bucket,
        "database_created": False,
        "bucket_created": False,
        "database_removed": False,
        "bucket_removed": False,
    }

    def s3_client():
        return get_session().create_client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
        )

    admin = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database="postgres",
        timeout=10,
    )
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        record["database_created"] = True
        settings.DB_NAME, settings.S3_BUCKET_NAME = database, bucket
        environment = dict(
            os.environ, RUN_HANDLER_ENV="test", DB_NAME=database, S3_BUCKET_NAME=bucket
        )
        async with s3_client() as s3:
            if any(
                item["Name"] == bucket for item in (await s3.list_buckets())["Buckets"]
            ):
                raise RuntimeError(
                    "Cold profiling bucket already exists; refusing to reuse it"
                )
            await s3.create_bucket(Bucket=bucket)
            record["bucket_created"] = True
        write_json(folder / "resources.json", record)
        with (folder / "migration.log").open("w") as log:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=60,
            )
        metadata["database"]["name"] = database
        metadata["database"]["configuration"] = dict(
            await admin.fetchrow(
                "SELECT current_setting('server_version') AS version, "
                "current_setting('max_connections') AS max_connections, "
                "current_setting('shared_buffers') AS shared_buffers"
            )
        )
        metadata["object_store"]["bucket"] = bucket
        yield environment
    finally:
        cleanup_errors = []
        try:
            if record["bucket_created"]:
                async with s3_client() as s3:
                    while True:
                        objects = (await s3.list_objects_v2(Bucket=bucket)).get(
                            "Contents", []
                        )
                        if not objects:
                            break
                        deleted = await s3.delete_objects(
                            Bucket=bucket,
                            Delete={
                                "Objects": [{"Key": item["Key"]} for item in objects]
                            },
                        )
                        if deleted.get("Errors"):
                            raise RuntimeError(
                                "Could not delete cold profiling objects"
                            )
                    await s3.delete_bucket(Bucket=bucket)
                record["bucket_removed"] = True
        except Exception as error:
            cleanup_errors.append(f"bucket: {type(error).__name__}: {error}")
        try:
            if record["database_created"]:
                await admin.execute(f'DROP DATABASE "{database}"')
                record["database_removed"] = True
        except Exception as error:
            cleanup_errors.append(f"database: {type(error).__name__}: {error}")
        finally:
            settings.DB_NAME, settings.S3_BUCKET_NAME = (
                original_database,
                original_bucket,
            )
            await admin.close()
            record["cleanup_errors"] = cleanup_errors
            write_json(folder / "resources.json", record)
        if cleanup_errors:
            raise RuntimeError(
                f"Cold profiling cleanup failed; see {folder / 'resources.json'}"
            )


async def collect(args, folder, settings, metadata):
    """Prepare/verify in this process; profile one request in a fresh child."""
    import httpx

    from ls_py_handler.main import app
    from scripts.measure import call, cpu_profile_directory, summarize, validate
    from scripts.measurement_workloads import fixture

    data = fixture(args.workloads[0], args.seed, args.smoke)
    cpu_folder = cpu_profile_directory(folder)
    target = None
    async with isolated_resources(settings, folder, metadata) as environment:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            if data["method"] == "GET":
                seed = await call(
                    client, {**data, "method": "POST"}, None, args.timeout
                )
                if seed.status_code != 201:
                    raise RuntimeError(
                        f"Cold GET seeding failed: HTTP {seed.status_code}"
                    )
                target = seed.json()["run_ids"][0]
            with tempfile.TemporaryDirectory(prefix="run-handler-cpu-") as temporary:
                config_path = Path(temporary) / "worker.json"
                body_path = Path(temporary) / "body.json"
                body_path.write_bytes(data["body"])
                write_json(
                    config_path,
                    {
                        "method": data["method"],
                        "target": target,
                        "body": str(body_path),
                        "timeout": args.timeout,
                        "profiles": str(cpu_folder),
                        "capture": str(folder / "capture.json"),
                    },
                )
                with (folder / "cold-worker.log").open("w") as log:
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "scripts.measurement_cpu",
                            str(config_path),
                        ],
                        cwd=ROOT,
                        env=environment,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=args.timeout + 60,
                    )
                if completed.returncode:
                    raise RuntimeError(
                        f"Cold CPU worker failed; see {folder / 'cold-worker.log'}"
                    )
            capture = json.loads((folder / "capture.json").read_text())
            response = httpx.Response(
                capture["status_code"], content=capture.pop("response_body").encode()
            )
            correct, reason = await validate(
                client, response, data, target, args.timeout
            )

    sample = {
        "request": 1,
        "duration_ns": capture["request_duration_ns"],
        "status": response.status_code,
        "outcome": "correct" if correct else "failure",
        "error": reason,
        "request_body_bytes": len(data["body"]) if data["method"] == "POST" else 0,
        "response_body_bytes": len(response.content),
    }
    if correct and data["method"] == "POST":
        sample["run_ids"] = response.json()["run_ids"]
    summary = summarize(
        [sample], sample["duration_ns"] / 1e9, data["batch_size"], data["method"]
    )
    summary.update(
        {
            "cpu_s": None,
            "cpu_s_per_request": None,
            "peak_allocated_mib": None,
            "profiled_cpu_s": capture["cpu_s"],
            **{
                f"profiled_{key}_cpu_s": value
                for key, value in capture["phase_cpu_s"].items()
            },
        }
    )
    return {
        "schema_version": 1,
        "stage": args.stage,
        "purpose": "harness-validation" if args.smoke else "experiment",
        "workload": ("smoke-" if args.smoke else "") + args.workloads[0],
        "pass": "cpu",
        "session": args.session,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "accepted": correct,
        "procedure": {
            "requests": 1,
            "warmup": 0,
            "sessions": args.sessions,
            "seed": args.seed,
            "concurrency": 1,
            "timeout_s": args.timeout,
            "transport": "in-process ASGI",
            "lifecycle": "fresh application process per capture",
            "cold_or_warm": (
                "cold application; operating system and services stay running"
            ),
            "timing_scope": (
                "first API request only; profile also includes imports/startup"
            ),
            "get_seed": "separate coordinator process",
            "get_target": target,
            "batch_size": data["batch_size"],
            "field_size_kb": data["field_size_kb"],
            "fixture_sha256": data["sha256"],
        },
        "environment": metadata,
        "summary": summary,
        "samples": [sample],
        "profile": {
            **capture,
            "mode": "cold",
            "artifact": str(cpu_folder / "cpu.pstat"),
            "text_artifact": str(cpu_folder / "cpu.txt"),
            "self_time_artifact": str(cpu_folder / "cpu-self.txt"),
        },
        "diagnostic": None,
    }


def worker(config_path):
    """Only stdlib/Yappi bootstrap and input reading precede profiling."""
    import yappi

    config = json.loads(Path(config_path).read_text())
    body = Path(config["body"]).read_bytes()
    assert os.environ.get("RUN_HANDLER_ENV") == "test"
    assert os.environ.get("DB_NAME", "").startswith("cpu_cold_")
    assert os.environ.get("S3_BUCKET_NAME", "").startswith("cpu-cold-")
    preloaded = [
        name
        for name in (
            "asyncio",
            "httpx",
            "fastapi",
            "asyncpg",
            "aiobotocore",
            "orjson",
            "ls_py_handler.main",
        )
        if name in sys.modules
    ]
    if preloaded:
        raise RuntimeError(f"Cold CPU dependencies already loaded: {preloaded}")
    capture = {
        "pid": os.getpid(),
        "preloaded_application_modules": preloaded,
        "clock": "cpu",
        "builtins": True,
        "includes_profiler_overhead": True,
        "scope": (
            "application imports, lifespan/client startup "
            "and first complete API response"
        ),
        "excluded": (
            "Python/Yappi bootstrap, fixtures, GET seeding, verification, shutdown"
        ),
    }
    error = None
    yappi.set_clock_type("cpu")
    yappi.start(builtins=True)
    cpu_start, wall_start = time.process_time_ns(), time.perf_counter_ns()
    try:
        import asyncio

        import httpx

        from ls_py_handler.main import app

        imports_cpu, imports_wall = time.process_time_ns(), time.perf_counter_ns()

        async def request():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    request_cpu, request_wall = (
                        time.process_time_ns(),
                        time.perf_counter_ns(),
                    )
                    async with asyncio.timeout(config["timeout"]):
                        if config["method"] == "POST":
                            response = await client.post(
                                "/runs",
                                content=body,
                                headers={"Content-Type": "application/json"},
                            )
                        else:
                            response = await client.get(f"/runs/{config['target']}")
                    end_cpu, end_wall = time.process_time_ns(), time.perf_counter_ns()
                    yappi.stop()
                    capture.update(
                        {
                            "cpu_s": (end_cpu - cpu_start) / 1e9,
                            "cpu_s_per_request": (end_cpu - cpu_start) / 1e9,
                            "elapsed_s": (end_wall - wall_start) / 1e9,
                            "request_duration_ns": end_wall - request_wall,
                            "phase_cpu_s": {
                                "imports": (imports_cpu - cpu_start) / 1e9,
                                "startup_and_client": (request_cpu - imports_cpu) / 1e9,
                                "first_request": (end_cpu - request_cpu) / 1e9,
                            },
                            "phase_elapsed_s": {
                                "imports": (imports_wall - wall_start) / 1e9,
                                "startup_and_client": (request_wall - imports_wall)
                                / 1e9,
                                "first_request": (end_wall - request_wall) / 1e9,
                            },
                            "status_code": response.status_code,
                            "response_body": response.text,
                        }
                    )

        asyncio.run(request())
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        yappi.stop()
        stats = yappi.get_func_stats()
        directory = Path(config["profiles"])
        stats.save(str(directory / "cpu.pstat"), type="pstat")
        for filename, order in (("cpu.txt", "ttot"), ("cpu-self.txt", "tsub")):
            with (directory / filename).open("w") as output:
                stats.sort(order).print_all(out=output)
        capture["error"] = error
        write_json(Path(config["capture"]), capture)
    if error:
        raise SystemExit(error)


if __name__ == "__main__":
    worker(sys.argv[1])
