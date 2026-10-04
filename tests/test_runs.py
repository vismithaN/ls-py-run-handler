import asyncio
import uuid
from unittest.mock import patch

import asyncpg
import orjson
import pytest
from aiobotocore.session import get_session
from fastapi.encoders import jsonable_encoder
from httpx import ASGITransport, AsyncClient

from ls_py_handler.api.routes.runs import Run, get_s3_client, run_batch_adapter
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
        assert response.headers["content-type"] == "application/json"
        assert int(response.headers["content-length"]) == len(response.content)
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
    conversion_counts = []
    copy_calls = []
    original_copy = asyncpg.Connection.copy_records_to_table

    async def counted_copy(db, table, **kwargs):
        copy_calls.append((table, kwargs["columns"]))
        return await original_copy(db, table, **kwargs)

    async def counted_s3_client():
        async for s3 in get_s3_client():
            with (
                patch.object(s3, "get_object", wraps=s3.get_object) as get_object,
                patch.object(orjson, "loads", wraps=orjson.loads) as loads,
                patch(
                    "fastapi.routing.jsonable_encoder", wraps=jsonable_encoder
                ) as convert,
            ):
                yield s3
                get_counts.append(get_object.call_count)
                decode_counts.append(loads.call_count)
                conversion_counts.append(convert.call_count)

    with (
        patch.dict(app.dependency_overrides, {get_s3_client: counted_s3_client}),
        patch.object(asyncpg.Connection, "copy_records_to_table", counted_copy),
        patch.object(
            run_batch_adapter, "validate_json", wraps=run_batch_adapter.validate_json
        ) as parse,
        patch(
            "starlette.requests.Request.json",
            side_effect=AssertionError("Separate JSON decoding must not run"),
        ),
    ):
        async with AsyncClient(app=app, base_url="http://test") as client:
            await assert_roundtrip(client, runs)
        assert parse.call_count == 1

    assert get_counts == [0, 1, 1, 1]  # POST, then one S3 GET per run.
    assert decode_counts == [0, 3, 3, 3]  # Decode three fields from one S3 read.
    assert conversion_counts == [1, 0, 0, 0]  # POST is unchanged; GET skips conversion.
    assert copy_calls == [
        ("runs", ("id", "trace_id", "name", "inputs", "outputs", "metadata"))
    ]  # One database COPY for the entire POST batch.


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
        # Preserve JSON media types, generated IDs and legacy byte encodings.
        for encoding, content_type in (
            ("utf-8", None),
            ("utf-8", "application/vnd.runs+json"),
            ("utf-8-sig", "application/json"),
            ("utf-16", "application/json"),
            ("utf-32", "application/json"),
        ):
            body = orjson.dumps(runs).decode().encode(encoding)
            headers = {"Content-Type": content_type} if content_type else {}
            response = await client.post("/runs", content=body, headers=headers)
            assert response.status_code == 201, response.text
            for sent, run_id in zip(runs, response.json()["run_ids"], strict=True):
                assert uuid.UUID(run_id).version == 4
                fetched = await client.get(f"/runs/{run_id}")
                assert fetched.json() == {**sent, "id": run_id}
    schema = app.openapi()["paths"]["/runs"]["post"]["requestBody"]
    assert schema["required"] is True
    assert schema["content"]["application/json"]["schema"]["type"] == "array"
    assert (
        schema["content"]["application/json"]["schema"]["items"]
        == Run.model_json_schema()
    )
    assert "422" in app.openapi()["paths"]["/runs"]["post"]["responses"]


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
        # Direct parsing has a lower nesting limit than the original decoder.
        # The fallback keeps previously supported, storable JSON working.
        nested = 0
        for _ in range(210):
            nested = [nested]
        run["inputs"] = {"deep": nested}
        run["id"] = str(uuid.uuid4())
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
        other_id = (await assert_roundtrip(client, [make_run(metadata=shared)]))[0]
    db = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
    )
    try:
        row = await db.fetchrow(
            "SELECT inputs, outputs, metadata FROM runs WHERE id = $1",
            uuid.UUID(ids[1]),
        )
        starts = [row[field].split("#", 1)[1].split(":", 1)[0] for field in row.keys()]
        assert len(set(starts)) == 3

        # A reference to a different batch object still uses the old read path.
        other_ref = await db.fetchval(
            "SELECT metadata FROM runs WHERE id = $1", uuid.UUID(other_id)
        )
        await db.execute(
            "UPDATE runs SET metadata = $1 WHERE id = $2",
            other_ref,
            uuid.UUID(ids[1]),
        )
    finally:
        await db.close()

    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get(f"/runs/{ids[1]}")
        assert response.status_code == 200
        assert response.json()["metadata"] == shared


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
            assert response.json()["detail"][0]["loc"] == ["body", 1, "trace_id"]

            for body, content_type, status_code in (
                (b"", "application/json", 422),
                (b"[", "application/json", 422),
                (b"{}", "application/json", 422),
                (b"null", "application/json", 422),
                (b"[]", "text/plain", 422),
                (b"\xff", "application/json", 400),
            ):
                response = await client.post(
                    "/runs", content=body, headers={"Content-Type": content_type}
                )
                assert response.status_code == status_code, response.text
            for fields in (
                {"trace_id": "not-a-uuid"},
                {"name": 123},
                {"inputs": []},
                {"metadata": None},
            ):
                invalid = {**make_run(), **fields}
                for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-32"):
                    response = await client.post(
                        "/runs",
                        content=orjson.dumps([invalid]).decode().encode(encoding),
                        headers={"Content-Type": "application/json"},
                    )
                    assert response.status_code == 422, response.text
                    assert response.json()["detail"][0]["loc"][:2] == ["body", 0]

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

            # Database failures must not commit the valid rows before a duplicate.
            existing = make_run("Existing ID")
            await assert_roundtrip(client, [existing])
            before_rows = await db.fetchval("SELECT count(*) FROM runs")
            duplicate = make_run("Duplicate within batch")
            failing_batches = [
                [
                    make_run("New first"),
                    make_run("Existing duplicate", id=existing["id"]),
                ],
                [duplicate, {**duplicate, "name": "Repeated ID"}],
            ]
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as failing_client:
                for batch in failing_batches:
                    response = await failing_client.post("/runs", json=batch)
                    assert response.status_code == 500
                    assert await db.fetchval("SELECT count(*) FROM runs") == before_rows
                    new_ids = [
                        uuid.UUID(run["id"])
                        for run in batch
                        if run["id"] != existing["id"]
                    ]
                    assert (
                        await db.fetchval(
                            "SELECT count(*) FROM runs WHERE id = ANY($1::uuid[])",
                            new_ids,
                        )
                        == 0
                    )
            response = await client.get(f"/runs/{existing['id']}")
            assert response.status_code == 200 and response.json() == existing
            await assert_roundtrip(client, [make_run("After failed database batch")])
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
                "SELECT inputs FROM runs WHERE id = $1", uuid.UUID(run["id"])
            )
            # Reference format: s3://bucket/object-key#start:end/field
            key = reference.split("#", 1)[0].split("/", 3)[3]
            await s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)

            response = await client.get(f"/runs/{run['id']}")
            assert response.status_code == 503, response.text
            assert response.json() == {"detail": "Stored run data is unavailable"}
            await assert_roundtrip(client, [make_run("After failed S3 read")])
    finally:
        await db.close()
