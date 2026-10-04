"""Offline grouping of existing Memray peaks; never reruns a workload.

Pass the route source from the measured revision, not today's application.
Each high-watermark record belongs to exactly one group. These are allocation
stack attributions, not exact object sizes or server-only memory measurements.
"""

from collections import defaultdict

GROUPING_VERSION = "python-stack-v1"


def allocation_group(stack, route_lines):
    if not stack:
        return "Unattributed (no Python stack)"
    if any("/botocore/" in path or "/aiobotocore/" in path for _, path, _ in stack):
        return "S3 SDK / data"
    if any(
        path.endswith("/tests/benchmarks/test_run_performance.py")
        and function.startswith("generate_")
        for function, path, _ in stack
    ):
        return "Benchmark-generated test data"

    function, path, _ = stack[0]
    route = next(
        (frame for frame in stack if frame[1].endswith("/api/routes/runs.py")), None
    )
    line = ""
    if route:
        if not 1 <= route[2] <= len(route_lines):
            raise ValueError("Allocation line is outside the measured route source")
        line = route_lines[route[2] - 1].strip()
        # model_dump is serialization, not inbound model validation.
        if "model_dump()" in line:
            if any(name == "model_dump" for name, _, _ in stack):
                return "Run dictionary construction"
            if "orjson.dumps" in line:
                return "Encoded run JSON"
            return "Run dictionary construction"

    if path.endswith(("/json/decoder.py", "/json/__init__.py")):
        return "Decoded request JSON"
    if function == "body" and path.endswith("/starlette/requests.py"):
        return "HTTP request body"
    if any(
        "/pydantic/" in filename
        or (name == "validate" and filename.endswith("/fastapi/_compat.py"))
        for name, filename, _ in stack
    ):
        return "Model validation"
    if path.endswith("/tests/benchmarks/test_run_performance.py"):
        return "Benchmark request serialization / bookkeeping"
    if "orjson.loads" in line:
        return "Decode retrieved field JSON"
    if route and route[0] == "create_runs":
        if "orjson.dumps(run_dicts)" in line:
            return "Serialize full batch"
        if "orjson.dumps" in line:
            return "Encoded field JSON"
        if "run_buffer" in line:
            return "Per-run buffer"
        if "batch.getvalue" in line or "batch.write" in line:
            return "Batch buffer"
        return "POST references / bookkeeping"
    if path.endswith(("/fastapi/encoders.py", "/starlette/responses.py")):
        return "HTTP response encoding"
    return "Other framework / client / runtime"


def grouped_allocations(recording, route_source):
    """Return descending groups and total bytes from a pinned recording."""
    import memray

    reader = memray.FileReader(recording)
    route_lines = route_source.splitlines()
    groups = defaultdict(int)
    for record in reader.get_high_watermark_allocation_records():
        groups[allocation_group(record.stack_trace(), route_lines)] += record.size
    total = sum(groups.values())
    if total != reader.metadata.peak_memory:
        raise ValueError("Allocation groups do not match the recorded peak")
    return (
        [
            {"label": label, "mib": size / 2**20}
            for label, size in sorted(
                groups.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        total,
    )
