import asyncio
import uuid
from unittest.mock import patch

import asyncpg
import orjson
import pytest
from aiobotocore.session import get_session
from httpx import AsyncClient

from ls_py_handler.api.routes.runs import Run, get_s3_client
from ls_py_handler.config.settings import settings
from ls_py_handler.main import app
from ls_py_handler.s3 import get_s3_session

pytestmark = pytest.mark.asyncio


def make_run(name="Test Run", **fields):
    """Build a request with explicit IDs and simple default data."""
    return Run(trace_id=uuid.uuid4(), name=name, **fields).model_dump(mode="json")


async def assert_roundtrip(client, runs):
    """Create the batch, then compare every returned run with its input."""
    response = await client.post("/runs", json=runs)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "created"
    ids = response.json()["run_ids"]
    assert len(ids) == len(runs)
    assert len(set(ids)) == len(ids)

    for sent, run_id in zip(runs, ids, strict=True):
        assert uuid.UUID(run_id).version == 4
        if "id" in sent:
            assert run_id == sent["id"]
        expected = {"inputs": {}, "outputs": {}, "metadata": {}, **sent, "id": run_id}
        response = await client.get(f"/runs/{run_id}")
        assert response.status_code == 200, response.text
        assert response.json() == expected
    return ids


async def test_create_and_get_run():
    """Existing batch test, strengthened to compare IDs and trace IDs too."""
    runs = [
        make_run(
            "Test Run 1",
            inputs={"prompt": "What is the capital of France?"},
            outputs={"answer": "Paris"},
            metadata={"model": "gpt-4", "temperature": 0.7},
        ),
        make_run(
            "Test Run 2",
            inputs={"prompt": "Tell me about machine learning"},
            outputs={"answer": "Machine learning is a branch of AI..."},
            metadata={"model": "gpt-3.5-turbo", "temperature": 0.5},
        ),
        make_run(
            "Test Run 3",
            inputs={"prompt": "Python code example"},
            outputs={"code": "print('Hello, World!')"},
            metadata={"model": "codex", "temperature": 0.2},
        ),
    ]
    get_counts = []
    decode_counts = []

    async def counted_s3_client():
        async for s3 in get_s3_client():
            with (
                patch.object(s3, "get_object", wraps=s3.get_object) as get_object,
                patch.object(orjson, "loads", wraps=orjson.loads) as loads,
            ):
                yield s3
                get_counts.append(get_object.call_count)
                decode_counts.append(loads.call_count)

    with patch.dict(app.dependency_overrides, {get_s3_client: counted_s3_client}):
        async with AsyncClient(app=app, base_url="http://test") as client:
            await assert_roundtrip(client, runs)

    assert get_counts == [0, 1, 1, 1]  # POST, then one S3 GET per run.
    assert decode_counts == [0, 1, 1, 1]  # Decode each complete run once.


async def test_s3_session_reused_but_clients_remain_request_scoped():
    session = get_s3_session()
    with patch.object(
        session, "create_client", wraps=session.create_client
    ) as create_client:
        async with AsyncClient(app=app, base_url="http://test") as client:
            await assert_roundtrip(client, [make_run("Session reuse")])

    assert get_s3_session() is session
    assert create_client.call_count == 2  # One client for POST, another for GET.


async def test_s3_session_handles_parallel_requests():
    session = get_s3_session()
    runs = [
        make_run(f"Parallel {index}", inputs={"index": index}) for index in range(3)
    ]
    with patch.object(
        session, "create_client", wraps=session.create_client
    ) as create_client:
        async with AsyncClient(app=app, base_url="http://test") as client:
            await asyncio.gather(*(assert_roundtrip(client, [run]) for run in runs))

    assert create_client.call_count == 6  # Each POST and GET has its own client.


async def test_generated_ids():
    runs = [
        make_run("First", inputs={"value": 1}),
        make_run("Second", inputs={"value": 2}),
    ]
    for run in runs:
        del run["id"]
    async with AsyncClient(app=app, base_url="http://test") as client:
        await assert_roundtrip(client, runs)


async def test_large_payload():
    run = make_run(
        inputs={"text": "i" * (100 * 1024)},
        outputs={"text": "o" * (100 * 1024)},
        metadata={"text": "m" * (100 * 1024)},
    )
    async with AsyncClient(app=app, base_url="http://test") as client:
        await assert_roundtrip(client, [run])


async def test_nested_json_and_special_characters():
    run = make_run(
        inputs={
            "nested": {"items": [None, True, False, 0, -7, 1.25, "", {"deep": []}]}
        },
        outputs={"text": '🌍 தமிழ் 中文 café "quotes" \\path\nnext\tcolumn'},
        metadata={"tags": ["one", "two"], "details": {"enabled": True}},
    )
    async with AsyncClient(app=app, base_url="http://test") as client:
        await assert_roundtrip(client, [run])


async def test_empty_and_omitted_fields():
    explicit = make_run("Empty", inputs={}, outputs={}, metadata={})
    omitted = make_run("Omitted")
    for field in ("inputs", "outputs", "metadata"):
        del omitted[field]
    async with AsyncClient(app=app, base_url="http://test") as client:
        await assert_roundtrip(client, [explicit, omitted])


async def test_repeated_field_content():
    shared = {"same": [1, "repeat", {"empty": {}}]}
    runs = [
        make_run("Nested", inputs={"wrapper": shared}, outputs=shared, metadata={}),
        make_run("Repeated", inputs=shared, outputs=shared, metadata=shared),
    ]
    async with AsyncClient(app=app, base_url="http://test") as client:
        ids = await assert_roundtrip(client, runs)
    db = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
    )
    try:
        rows = [
            await db.fetchrow(
                "SELECT run_ref, inputs, outputs, metadata FROM runs WHERE id = $1",
                uuid.UUID(run_id),
            )
            for run_id in ids
        ]
        assert rows[0]["run_ref"] != rows[1]["run_ref"]
        for row in rows:
            assert row["run_ref"].endswith("/run")
            assert all(
                row[field] is None for field in ("inputs", "outputs", "metadata")
            )

        # Seed an old-format row, including a field in a different S3 object.
        legacy = make_run("Legacy run", inputs=shared, outputs=shared, metadata=shared)
        body = orjson.dumps([legacy])
        key = f"legacy/{uuid.uuid4()}.json"
        metadata_key = f"legacy/{uuid.uuid4()}.json"
        refs = {}
        for field in ("inputs", "outputs", "metadata"):
            marker = orjson.dumps(field) + b":"
            start = body.index(marker) + len(marker)
            end = start + len(orjson.dumps(legacy[field]))
            refs[field] = f"s3://{settings.S3_BUCKET_NAME}/{key}#{start}:{end}/{field}"
        metadata_body = orjson.dumps(shared)
        refs["metadata"] = (
            f"s3://{settings.S3_BUCKET_NAME}/{metadata_key}"
            f"#0:{len(metadata_body)}/metadata"
        )
        async with get_session().create_client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
        ) as s3:
            await s3.put_object(Bucket=settings.S3_BUCKET_NAME, Key=key, Body=body)
            await s3.put_object(
                Bucket=settings.S3_BUCKET_NAME, Key=metadata_key, Body=metadata_body
            )
        await db.execute(
            """INSERT INTO runs (id, trace_id, name, inputs, outputs, metadata)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            uuid.UUID(legacy["id"]),
            uuid.UUID(legacy["trace_id"]),
            legacy["name"],
            refs["inputs"],
            refs["outputs"],
            refs["metadata"],
        )
    finally:
        await db.close()

    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get(f"/runs/{legacy['id']}")
        assert response.status_code == 200
        assert response.json() == legacy


async def test_invalid_requests_and_missing_runs():
    db = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        timeout=5,
    )
    try:
        async with get_session().create_client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
        ) as s3, AsyncClient(app=app, base_url="http://test") as client:
            before_rows = await db.fetchval("SELECT count(*) FROM runs")
            before_keys = set()
            async for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=settings.S3_BUCKET_NAME
            ):
                before_keys.update(item["Key"] for item in page.get("Contents", []))

            response = await client.post("/runs", json=[])
            assert response.status_code == 400

            invalid = make_run("Missing trace")
            del invalid["trace_id"]
            response = await client.post("/runs", json=[make_run("Valid"), invalid])
            assert response.status_code == 422

            assert await db.fetchval("SELECT count(*) FROM runs") == before_rows
            after_keys = set()
            async for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=settings.S3_BUCKET_NAME
            ):
                after_keys.update(item["Key"] for item in page.get("Contents", []))
            assert after_keys == before_keys

            response = await client.get("/runs/not-a-uuid")
            assert response.status_code == 422
            response = await client.get(f"/runs/{uuid.uuid4()}")
            assert response.status_code == 404
    finally:
        await db.close()


async def test_missing_storage_object():
    # This test deletes only the object for the run it creates, in the test bucket.
    assert settings.S3_BUCKET_NAME == "runs-test" or settings.S3_BUCKET_NAME.startswith(
        "runs-test-"
    ), "Use a dedicated test bucket"
    db = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        timeout=5,
    )
    try:
        async with get_session().create_client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
        ) as s3, AsyncClient(app=app, base_url="http://test") as client:
            run = make_run(inputs={"prompt": "hello"}, outputs={"answer": "world"})
            await assert_roundtrip(client, [run])
            reference = await db.fetchval(
                "SELECT run_ref FROM runs WHERE id = $1", uuid.UUID(run["id"])
            )
            # Reference format: s3://bucket/object-key#start:end/run
            key = reference.split("#", 1)[0].split("/", 3)[3]
            await s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)

            response = await client.get(f"/runs/{run['id']}")
            assert response.status_code == 503, response.text
            assert response.json() == {"detail": "Stored run data is unavailable"}

            original_body = orjson.dumps([run])
            wrong_run = {**run, "id": str(uuid.uuid4())}
            for damaged_body in (
                original_body[:-2],  # Truncated S3 range.
                b"[" + b" " * (len(original_body) - 2) + b"]",  # Invalid run JSON.
                orjson.dumps([wrong_run]),  # Valid JSON, but a different run.
            ):
                await s3.put_object(
                    Bucket=settings.S3_BUCKET_NAME, Key=key, Body=damaged_body
                )
                response = await client.get(f"/runs/{run['id']}")
                assert response.status_code == 503, response.text

            await s3.put_object(
                Bucket=settings.S3_BUCKET_NAME, Key=key, Body=original_body
            )
            response = await client.get(f"/runs/{run['id']}")
            assert response.status_code == 200
            assert response.json() == run
            await assert_roundtrip(client, [make_run("After failed S3 read")])
    finally:
        await db.close()
