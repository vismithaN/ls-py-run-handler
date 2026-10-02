"""Public API correctness; intentionally independent of storage encoding."""

import copy
import uuid

import pytest

from ls_py_handler.config.settings import settings

pytestmark = pytest.mark.asyncio


async def assert_roundtrip(client, runs):
    response = await client.post("/runs", json=runs)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "created"
    ids = response.json()["run_ids"]
    assert len(ids) == len(runs)
    assert len(set(ids)) == len(ids)
    for sent, run_id in zip(runs, ids, strict=False):
        assert uuid.UUID(run_id).version == 4
        if "id" in sent:
            assert run_id == sent["id"]
        expected = {"inputs": {}, "outputs": {}, "metadata": {}, **sent, "id": run_id}
        fetched = await client.get(f"/runs/{run_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json() == expected
    return ids


@pytest.mark.parametrize("count", [1, 3])
async def test_exact_roundtrip(client, make_run, count):
    await assert_roundtrip(
        client, [make_run(f"roundtrip-{count}", i) for i in range(count)]
    )


async def test_generated_ids(client, make_run):
    runs = [make_run("generated", i) for i in range(3)]
    for run in runs:
        del run["id"]
    first = await assert_roundtrip(client, runs)
    second = await assert_roundtrip(client, runs)
    assert set(first).isdisjoint(second)


@pytest.mark.parametrize("count,size_kb", [(500, 10), (50, 100)])
async def test_supplied_post_workload_integrity(client, make_run, count, size_kb):
    # Match the supplied generator's 100-character dictionary chunks and nominal
    # field sizes; actual serialized bytes include keys and JSON punctuation.
    runs = []
    for index in range(count):
        fields = {}
        for field in ("inputs", "outputs", "metadata"):
            prefix = f"{field}-{index:04d}-"
            text = prefix + "x" * (size_kb * 1024 - len(prefix))
            fields[field] = {
                f"key_{offset // 100}": text[offset : offset + 100]
                for offset in range(0, len(text), 100)
            }
        runs.append(make_run(f"large-{count}-{size_kb}", index, **fields))
    await assert_roundtrip(client, runs)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"nested": {"items": [None, True, False, 0, -7, 1.25, "", {"deep": []}]}},
        {"text": '🌍 தமிழ் 中文 café "quotes" \\path\nnext\tcolumn'},
    ],
    ids=["empty", "nested-types", "unicode-escaping"],
)
async def test_json_values(client, make_run, value, request):
    await assert_roundtrip(
        client,
        [make_run(request.node.name, inputs=value, outputs=value, metadata=value)],
    )


async def test_omitted_fields_default_independently(client, make_run):
    runs = [make_run("defaults", i) for i in range(3)]
    for field in ("inputs", "outputs", "metadata"):
        del runs[0][field]
        del runs[2][field]
    await assert_roundtrip(client, runs)


async def test_repeated_and_nested_content(client, make_run):
    shared = {"same": [1, "repeat", {"empty": {}}]}
    runs = [
        make_run(
            "repeated", 0, inputs={"wrapper": shared}, outputs=shared, metadata={}
        ),
        make_run("repeated", 1, inputs=shared, outputs=shared, metadata=shared),
        make_run(
            "repeated", 2, inputs={}, outputs={"wrapper": shared}, metadata=shared
        ),
    ]
    await assert_roundtrip(client, copy.deepcopy(runs))


@pytest.mark.parametrize(
    "case,status",
    [
        ("empty-batch", 400),
        ("malformed-json", 422),
        ("object-not-list", 422),
        ("missing-name", 422),
        ("missing-trace", 422),
        ("invalid-id", 422),
        ("invalid-trace", 422),
        ("non-object-field", 422),
        ("mixed-valid-invalid", 422),
    ],
)
async def test_rejected_input_has_no_writes(client, db, s3, make_run, case, status):
    before_rows = await db.fetchval("SELECT count(*) FROM runs")
    before_keys = await object_keys(s3)
    run = make_run(case)
    payload = [run]
    if case == "empty-batch":
        payload = []
    elif case == "object-not-list":
        payload = run
    elif case == "missing-name":
        del run["name"]
    elif case == "missing-trace":
        del run["trace_id"]
    elif case == "invalid-id":
        run["id"] = "not-a-uuid"
    elif case == "invalid-trace":
        run["trace_id"] = "not-a-uuid"
    elif case == "non-object-field":
        run["inputs"] = "not-an-object"
    elif case == "mixed-valid-invalid":
        payload.append({"name": "missing trace ID"})
    if case == "malformed-json":
        response = await client.post(
            "/runs", content=b"[{broken", headers={"Content-Type": "application/json"}
        )
    else:
        response = await client.post("/runs", json=payload)
    assert response.status_code == status, response.text
    assert await db.fetchval("SELECT count(*) FROM runs") == before_rows
    assert await object_keys(s3) == before_keys


async def object_keys(s3):
    keys = set()
    async for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=settings.S3_BUCKET_NAME
    ):
        keys.update(item["Key"] for item in page.get("Contents", []))
    return keys


async def test_missing_run(client, make_run):
    response = await client.get(f"/runs/{make_run('absent')['id']}")
    assert response.status_code == 404


async def test_invalid_get_uuid(client):
    response = await client.get("/runs/not-a-uuid")
    assert response.status_code == 422
