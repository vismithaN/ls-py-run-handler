"""The four supplied workload shapes, with fixed seed and request sequence."""

import hashlib
import random
import string
import uuid

import orjson

WORKLOADS = {
    "post-500-10kb": ("POST", 500, 10),
    "post-50-100kb": ("POST", 50, 100),
    "get-10kb": ("GET", 1, 10),
    "get-100kb": ("GET", 1, 100),
}


def make_runs(count, size_kb, seed):
    rng = random.Random(seed)

    def new_uuid():
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))

    def field():
        # Match the supplied benchmarks: 100-character dictionary values.
        text = "".join(
            rng.choice(string.ascii_letters + string.digits)
            for _ in range(size_kb * 1024)
        )
        return {f"key_{i // 100}": text[i : i + 100] for i in range(0, len(text), 100)}

    return [
        {
            "trace_id": new_uuid(),
            "name": f"Benchmark Run {new_uuid()}",
            "inputs": field(),
            "outputs": field(),
            "metadata": field(),
        }
        for _ in range(count)
    ]


def fixture(workload, seed, smoke=False):
    method, count, size_kb = WORKLOADS[workload]
    if smoke:
        count, size_kb = (2 if method == "POST" else 1), 1
    runs = make_runs(count, size_kb, seed)
    body = orjson.dumps(runs)
    # Omit IDs just like the supplied tests. The server generates unique IDs on
    # every POST; validation uses returned IDs. Payload/trace-ID sequence is frozen.
    return {
        "method": method,
        "runs": runs,
        "body": body,
        "batch_size": count,
        "field_size_kb": size_kb,
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def expected_run(run, run_id):
    return {"id": run_id, **run}
