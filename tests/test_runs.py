"""Integration coverage for run values, parsing, storage failures and concurrency."""

import asyncio
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
    assert settings.DB_NAME == "postgres_test" or settings.DB_NAME.startswith(
        "postgres_test_"
    ), "Use a dedicated test database"
    assert settings.S3_BUCKET_NAME == "runs-test" or settings.S3_BUCKET_NAME.startswith(
        "runs-test-"
    ), "Use a dedicated test bucket"
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


async def assert_roundtrip(client, runs):
    response = await client.post("/runs", json=runs)
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
                make_run(),
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
    await assert_roundtrip(client, runs)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
async def test_fragmented_json(client, encoding):
    run = make_run(
        inputs={
            "nested": [
                None,
                True,
                False,
                0,
                -7,
                1.25,
                "",
                {"deep": []},
                -(2**63),
                2**64 - 1,
                1e300,
                -0.0,
            ]
        },
        outputs={"text": '🌍 தமிழ் 中文 café "quotes" \\path\nnext\tcolumn'},
        metadata={"tags": ["one", "two"], "details": {"enabled": True}},
    )
    payload = orjson.dumps([run]).decode().encode(encoding)

    async def chunks():
        for start in range(0, len(payload), 7):
            yield payload[start : start + 7]

    response = await client.post(
        "/runs", content=chunks(), headers={"Content-Type": "application/vendor+json"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["run_ids"] == [run["id"]]
    response = await client.get(f"/runs/{run['id']}")
    assert response.status_code == 200, response.text
    assert response.json() == run


async def storage_snapshot(db, s3):
    keys = set()
    async for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=settings.S3_BUCKET_NAME
    ):
        keys.update(item["Key"] for item in page.get("Contents", []))
    return await db.fetchval("SELECT count(*) FROM runs"), keys


VALID_BATCH = orjson.dumps([make_run()])


@pytest.mark.parametrize(
    "body,expected_status",
    [
        pytest.param(b"[]", 400, id="empty-batch"),
        pytest.param(VALID_BATCH[:-1], 422, id="truncated-batch"),
        pytest.param(b"{}", 422, id="non-array-root"),
        pytest.param(
            orjson.dumps([make_run(), {"name": "Invalid"}]),
            422,
            id="invalid-run-after-valid-run",
        ),
    ],
)
async def test_rejected_batch_writes_nothing(client, storage, body, expected_status):
    before = await storage_snapshot(*storage)
    response = await client.post(
        "/runs", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 422:
        assert response.json()["detail"]
    assert await storage_snapshot(*storage) == before


@pytest.mark.parametrize("duplicate", ["existing-id", "within-batch"])
async def test_failed_batch_rolls_back(client, storage, duplicate):
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
    db, s3 = storage
    assert (await client.get("/runs/not-a-uuid")).status_code == 422
    assert (await client.get(f"/runs/{uuid.uuid4()}")).status_code == 404
    run = make_run(inputs={"prompt": "hello"}, outputs={"answer": "world"})
    await assert_roundtrip(client, [run])
    reference = await db.fetchval(
        "SELECT inputs FROM runs WHERE id = $1", uuid.UUID(run["id"])
    )
    key = reference.split("#", 1)[0].split("/", 3)[3]
    await s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
    response = await client.get(f"/runs/{run['id']}")
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Stored run data is unavailable"}
    await assert_roundtrip(client, [make_run()])


async def test_parallel_roundtrips(client):
    runs = [make_run(inputs={"request": i}) for i in range(3)]
    await asyncio.gather(*(assert_roundtrip(client, [run]) for run in runs))
