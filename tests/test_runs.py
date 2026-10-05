"""Correctness coverage for run round trips, rejected writes and storage failures."""

import uuid

import asyncpg
import orjson
import pytest
import pytest_asyncio
from aiobotocore.session import get_session
from httpx import ASGITransport, AsyncClient

from ls_py_handler.config.settings import settings
from ls_py_handler.main import app

pytestmark = pytest.mark.asyncio


def make_run(**fields):
    """Build a valid run, with overrides for the scenario being tested."""
    return {
        "id": str(uuid.uuid4()),
        "trace_id": str(uuid.uuid4()),
        "name": "Test Run",
        "inputs": {},
        "outputs": {},
        "metadata": {},
        **fields,
    }


@pytest_asyncio.fixture
async def client():
    """Send requests directly to the app, using only dedicated test storage."""
    assert settings.DB_NAME == "postgres_test" or settings.DB_NAME.startswith(
        "postgres_test_"
    ), "Use a dedicated test database"
    assert settings.S3_BUCKET_NAME == "runs-test" or settings.S3_BUCKET_NAME.startswith(
        "runs-test-"
    ), "Use a dedicated test bucket"
    # Return HTTP 500 responses so failed-batch tests can inspect them.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def storage(client):
    """Inspect only the test resources guarded by the client fixture."""
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
        ) as s3:
            yield db, s3
    finally:
        await db.close()


async def body_chunks(body):
    """Fragment requests, including a split inside the test emoji's UTF-8 bytes."""
    yield body[:1]
    body = body[1:]
    split = body.find("🙂".encode())
    if split >= 0:
        yield body[: split + 1]
        body = body[split + 1 :]
    for start in range(0, len(body), 16 * 1024):
        yield body[start : start + 16 * 1024]


async def assert_roundtrip(client, runs):
    """Create a batch, then retrieve each run and compare its full contents."""
    response = await client.post(
        "/runs",
        content=body_chunks(orjson.dumps(runs)),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["status"] == "created"
    ids = result["run_ids"]
    assert len(ids) == len(runs) == len(set(ids))
    for sent, run_id in zip(runs, ids, strict=True):
        assert uuid.UUID(run_id).version == 4
        assert run_id == sent.get("id", run_id)
        expected = {"inputs": {}, "outputs": {}, "metadata": {}, **sent, "id": run_id}
        response = await client.get(f"/runs/{run_id}")
        assert response.status_code == 200, response.text
        assert response.json() == expected


SHARED = {"same": [1, "repeat", {"empty": {}}]}
LARGE = {"text": "x" * (100 * 1024)}


@pytest.mark.parametrize(
    "runs",
    [
        pytest.param(
            [
                {
                    k: v
                    for k, v in make_run().items()
                    if k not in ("id", "inputs", "outputs", "metadata")
                },
                make_run(name="Test Run 🙂"),
            ],
            id="generated-id-and-default-fields",
        ),
        pytest.param(
            [
                make_run(inputs=LARGE, outputs=LARGE, metadata=LARGE),
                make_run(),
                make_run(outputs=LARGE),
            ],
            id="large-small-large",
        ),
        pytest.param(
            [
                make_run(inputs={"wrapper": SHARED}, outputs=SHARED),
                make_run(inputs=SHARED, outputs=SHARED, metadata=SHARED),
            ],
            id="repeated-and-nested-content",
        ),
    ],
)
async def test_batch_roundtrip(client, runs):
    """Verify POST/GET preserve IDs and exact field values.

    Cover IDs, defaults, varied sizes and repeated content over fragmented input.
    """
    await assert_roundtrip(client, runs)


async def storage_snapshot(db, s3):
    """Record row count and object keys to detect writes from rejected requests."""
    keys = set()
    async for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=settings.S3_BUCKET_NAME
    ):
        keys.update(item["Key"] for item in page.get("Contents", []))
    return await db.fetchval("SELECT count(*) FROM runs"), keys


VALID_BATCH = orjson.dumps([make_run()])


@pytest.mark.parametrize(
    "bodies,expected_status",
    [
        pytest.param([b"[]"], 400, id="empty-batch"),
        pytest.param(
            [VALID_BATCH[:-1], VALID_BATCH + b" extra", VALID_BATCH[:-1] + b",]"],
            422,
            id="malformed-batch",
        ),
        pytest.param([b"{}"], 422, id="non-array-root"),
        pytest.param(
            [orjson.dumps([make_run(), {"name": "Invalid"}])],
            422,
            id="invalid-run-after-valid-run",
        ),
    ],
)
async def test_rejected_batch_writes_nothing(client, storage, bodies, expected_status):
    """Reject empty, malformed or invalid batches with the expected HTTP error.

    Neither database rows nor S3 object keys should change after rejection.
    """
    before = await storage_snapshot(*storage)
    for body in bodies:
        response = await client.post(
            "/runs",
            content=body_chunks(body),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == expected_status, response.text
        if expected_status == 422:
            assert response.json()["detail"]
        assert await storage_snapshot(*storage) == before


@pytest.mark.parametrize("duplicate", ["existing-id", "within-batch"])
async def test_failed_batch_rolls_back(client, storage, duplicate):
    """Reject the whole batch when an ID duplicates an existing or incoming run.

    Preserve existing data and allow a subsequent valid request to succeed.
    """
    db, _ = storage
    existing = make_run()
    await assert_roundtrip(client, [existing])
    first = make_run()
    second = make_run(id=existing["id"] if duplicate == "existing-id" else first["id"])
    before = await db.fetchval("SELECT count(*) FROM runs")
    response = await client.post("/runs", json=[first, second])
    assert response.status_code == 500, response.text
    assert await db.fetchval("SELECT count(*) FROM runs") == before
    assert (
        await db.fetchval(
            "SELECT count(*) FROM runs WHERE id = $1", uuid.UUID(first["id"])
        )
        == 0
    )
    response = await client.get(f"/runs/{existing['id']}")
    assert response.status_code == 200 and response.json() == existing
    await assert_roundtrip(client, [make_run()])


async def test_missing_runs_and_objects(client, storage):
    """Check ID errors, legacy reads, and 503 for invalid or missing stored data."""
    db, s3 = storage
    assert (await client.get("/runs/not-a-uuid")).status_code == 422
    assert (await client.get(f"/runs/{uuid.uuid4()}")).status_code == 404
    run = make_run(inputs={"prompt": "hello"}, outputs={"answer": "world"})
    await assert_roundtrip(client, [run])
    run_id = uuid.UUID(run["id"])
    references = await db.fetchrow(
        "SELECT inputs, outputs FROM runs WHERE id = $1", run_id
    )
    reference = references["inputs"]
    location = reference.split("#", 1)[0]
    # Invalid references must not become successful responses with empty fields.
    for invalid in (
        None,
        "",
        "not-an-s3-reference",
        location,
        f"{location}#bad:2/inputs",
        f"{location}#-1:2/inputs",
        f"{location}#2:2/inputs",
        f"{location}#3:2/inputs",
        f"{location}#0:2/outputs",
    ):
        await db.execute("UPDATE runs SET inputs = $1 WHERE id = $2", invalid, run_id)
        response = await client.get(f"/runs/{run['id']}")
        assert response.status_code == 503, response.text
        assert response.json() == {"detail": "Stored run data is unavailable"}
    await db.execute("UPDATE runs SET inputs = $1 WHERE id = $2", reference, run_id)

    # An older run can store a field in a different S3 object.
    legacy_key = f"legacy/{run['id']}.json"
    legacy_data = orjson.dumps(run["outputs"])
    await s3.put_object(
        Bucket=settings.S3_BUCKET_NAME, Key=legacy_key, Body=legacy_data
    )
    legacy_ref = (
        f"s3://{settings.S3_BUCKET_NAME}/{legacy_key}#0:{len(legacy_data)}/outputs"
    )
    await db.execute("UPDATE runs SET outputs = $1 WHERE id = $2", legacy_ref, run_id)
    response = await client.get(f"/runs/{run['id']}")
    assert response.status_code == 200, response.text
    assert response.json() == run
    await db.execute(
        "UPDATE runs SET outputs = $1 WHERE id = $2", references["outputs"], run_id
    )
    await s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=legacy_key)

    key = reference.split("#", 1)[0].split("/", 3)[3]
    # Keep the database row but remove its object to simulate missing stored data.
    await s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
    response = await client.get(f"/runs/{run['id']}")
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Stored run data is unavailable"}
    await assert_roundtrip(client, [make_run()])
