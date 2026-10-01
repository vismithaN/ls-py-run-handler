import uuid

import asyncpg
import orjson
import pytest
import pytest_asyncio
from botocore.exceptions import ClientError
from httpx import AsyncClient

from ls_py_handler.config.settings import settings
from ls_py_handler.main import app
from ls_py_handler.resources import get_db_conn, get_s3_client


@pytest_asyncio.fixture
async def client():
    async with app.router.lifespan_context(app):
        async with AsyncClient(app=app, base_url="http://test") as test_client:
            yield test_client


def run_payload(**overrides):
    payload = {
        "id": str(uuid.uuid4()),
        "trace_id": str(uuid.uuid4()),
        "name": "Test Run",
        "inputs": {"prompt": "What is the capital of France?"},
        "outputs": {"answer": "Paris"},
        "metadata": {"model": "gpt-4", "temperature": 0.7},
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_and_get_runs_preserves_exact_payload(client):
    shared = {"value": "same value", "nested": {"emoji": "こんにちは 🌍"}}
    runs = [
        run_payload(name="First", inputs=shared, outputs=shared, metadata=shared),
        run_payload(name="Second", inputs=shared, outputs=shared, metadata=shared),
        run_payload(
            name="Escapes",
            inputs={"text": 'line one\nline two\\path"quoted"'},
            outputs={"number": 42, "enabled": True},
            metadata={"none": None},
        ),
    ]

    response = await client.post("/runs", json=runs)

    assert response.status_code == 201
    assert response.json() == {
        "status": "created",
        "run_ids": [run["id"] for run in runs],
    }
    assert response.headers["X-Request-ID"]

    for expected in runs:
        get_response = await client.get(f"/runs/{expected['id']}")
        assert get_response.status_code == 200
        assert get_response.json() == expected


@pytest.mark.asyncio
async def test_create_rejects_empty_batch(client):
    response = await client.post("/runs", json=[])

    assert response.status_code == 400
    assert response.json() == {"detail": "No runs provided"}


@pytest.mark.asyncio
async def test_create_rejects_too_many_runs(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_BATCH_RUNS", 1)

    response = await client.post("/runs", json=[run_payload(), run_payload()])

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_create_rejects_oversized_request_before_parsing(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_REQUEST_BYTES", 1)

    response = await client.post("/runs", json=[run_payload()])

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body exceeds the configured limit"}


@pytest.mark.asyncio
async def test_object_storage_failure_returns_503_without_database_row(client):
    payload = run_payload()

    class FailingS3:
        async def put_object(self, **_kwargs):
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "injected"}},
                "PutObject",
            )

    app.dependency_overrides[get_s3_client] = lambda: FailingS3()
    try:
        response = await client.post("/runs", json=[payload])
    finally:
        app.dependency_overrides.pop(get_s3_client, None)

    assert response.status_code == 503
    assert (await client.get(f"/runs/{payload['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_database_failure_removes_uploaded_object(client):
    class FailingTransaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    class FailingConnection:
        def transaction(self):
            return FailingTransaction()

        async def copy_records_to_table(self, *_args, **_kwargs):
            raise asyncpg.PostgresError("injected")

    async def failing_db():
        yield FailingConnection()

    resources = app.state.resources
    before = await resources.s3.list_objects_v2(Bucket=settings.S3_BUCKET_NAME)
    app.dependency_overrides[get_db_conn] = failing_db
    try:
        response = await client.post("/runs", json=[run_payload()])
    finally:
        app.dependency_overrides.pop(get_db_conn, None)
    after = await resources.s3.list_objects_v2(Bucket=settings.S3_BUCKET_NAME)

    assert response.status_code == 503
    assert after.get("KeyCount", 0) == before.get("KeyCount", 0)


@pytest.mark.asyncio
async def test_duplicate_id_is_atomic_and_uploaded_object_is_removed(client):
    existing = run_payload()
    assert (await client.post("/runs", json=[existing])).status_code == 201

    resources = app.state.resources
    before = await resources.s3.list_objects_v2(Bucket=settings.S3_BUCKET_NAME)
    fresh = run_payload()
    response = await client.post("/runs", json=[fresh, existing])
    after = await resources.s3.list_objects_v2(Bucket=settings.S3_BUCKET_NAME)

    assert response.status_code == 409
    assert response.json() == {"detail": "One or more run IDs already exist"}
    assert (await client.get(f"/runs/{fresh['id']}")).status_code == 404
    assert (await client.get(f"/runs/{existing['id']}")).status_code == 200
    assert after.get("KeyCount", 0) == before.get("KeyCount", 0)


@pytest.mark.asyncio
async def test_missing_object_is_reported_as_integrity_error(client):
    payload = run_payload()
    assert (await client.post("/runs", json=[payload])).status_code == 201

    resources = app.state.resources
    async with resources.db_pool.acquire() as connection:
        object_key = await connection.fetchval(
            "SELECT object_key FROM runs WHERE id = $1", uuid.UUID(payload["id"])
        )
    await resources.s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=object_key)

    response = await client.get(f"/runs/{payload['id']}")

    assert response.status_code == 500
    assert response.json() == {"detail": "Stored run data is unavailable"}


@pytest.mark.asyncio
async def test_legacy_version_one_row_remains_readable(client):
    payload = run_payload(name="Legacy")
    fields = [payload["inputs"], payload["outputs"], payload["metadata"]]
    encoded_fields = [orjson.dumps(field) for field in fields]
    body = b"".join(encoded_fields)
    key = f"legacy/{payload['id']}.json"

    resources = app.state.resources
    await resources.s3.put_object(
        Bucket=settings.S3_BUCKET_NAME,
        Key=key,
        Body=body,
        ContentLength=len(body),
    )
    starts = [
        0,
        len(encoded_fields[0]),
        len(encoded_fields[0]) + len(encoded_fields[1]),
    ]
    references = [
        f"s3://{settings.S3_BUCKET_NAME}/{key}#{start}:{start + len(value)}/{field}"
        for start, value, field in zip(
            starts, encoded_fields, ("inputs", "outputs", "metadata"), strict=True
        )
    ]
    async with resources.db_pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO runs (id, trace_id, name, inputs, outputs, metadata)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            uuid.UUID(payload["id"]),
            uuid.UUID(payload["trace_id"]),
            payload["name"],
            *references,
        )

    response = await client.get(f"/runs/{payload['id']}")

    assert response.status_code == 200
    assert response.json() == payload


@pytest.mark.asyncio
async def test_get_unknown_run_returns_404(client):
    run_id = uuid.uuid4()

    response = await client.get(f"/runs/{run_id}")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_health_endpoints(client):
    assert (await client.get("/health/live")).json() == {"status": "alive"}

    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
