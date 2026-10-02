"""Failure injection against real storage, without changing application code."""

import uuid

import pytest

from ls_py_handler.api.routes import runs as routes
from ls_py_handler.config.settings import settings
from ls_py_handler.main import app

from .test_roundtrip import object_keys

pytestmark = pytest.mark.asyncio


async def test_failed_upload_does_not_insert_rows(client, db, s3, make_run):
    class FailedUpload:
        called = False

        async def put_object(self, **kwargs):
            self.called = True
            raise ConnectionError("Injected upload failure before object creation")

    proxy = FailedUpload()

    async def failed_s3():
        yield proxy

    app.dependency_overrides[routes.get_s3_client] = failed_s3
    before_rows = await db.fetchval("SELECT count(*) FROM runs")
    before_keys = await object_keys(s3)
    response = await client.post("/runs", json=[make_run("failed-upload")])
    assert response.status_code == 500
    assert proxy.called
    assert await db.fetchval("SELECT count(*) FROM runs") == before_rows
    assert await object_keys(s3) == before_keys


async def test_database_failure_characterization(
    client, db, s3, make_run, record_property
):
    class FailSecondInsert:
        calls = 0

        async def fetchval(self, query, *args):
            self.calls += 1
            if self.calls == 2:
                raise ConnectionError("Injected database failure at second insert")
            return await db.fetchval(query, *args)

    proxy = FailSecondInsert()

    async def failing_db():
        yield proxy

    app.dependency_overrides[routes.get_db_conn] = failing_db
    payload = [make_run("db-failure", i) for i in range(3)]
    before_keys = await object_keys(s3)
    response = await client.post("/runs", json=payload)
    assert response.status_code == 500
    assert proxy.calls == 2  # Confirm the intended fault was exercised.
    persisted = await db.fetchval(
        "SELECT count(*) FROM runs WHERE id = ANY($1::uuid[])",
        [uuid.UUID(run["id"]) for run in payload],
    )
    created_keys = await object_keys(s3) - before_keys
    record_property("persisted_runs_after_failed_batch", persisted)
    record_property("objects_remaining_after_failed_batch", len(created_keys))
    # Observations, not assertions that partial writes must remain forever.
    print(
        f"DB failure: {persisted}/3 runs persisted; {len(created_keys)} objects remain"
    )


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_storage_read_failure_is_not_silent_success(
    client, db, s3, make_run, damage, record_property
):
    run = make_run(f"read-failure-{damage}")
    created = await client.post("/runs", json=[run])
    assert created.status_code == 201
    original = await client.get(f"/runs/{run['id']}")
    assert original.status_code == 200
    assert original.json() == run
    ref = await db.fetchval(
        "SELECT inputs FROM runs WHERE id = $1", uuid.UUID(run["id"])
    )
    key = ref.split("#", 1)[0].split("/", 3)[3]
    if damage == "missing":
        await s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
    else:
        info = await s3.head_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
        # Preserve length so byte ranges are valid but JSON decoding must fail.
        await s3.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=key,
            Body=b"!" * info["ContentLength"],
        )
    response = await client.get(f"/runs/{run['id']}")
    record_property("http_status_after_storage_damage", response.status_code)
    record_property("response_after_storage_damage", response.text)
    # No exact error policy is imposed yet, but 200 with fabricated empty fields
    # must not pass as a correct retrieval. Baseline code inspection predicts
    # this assertion will fail; do not hide it behind an unconditional xfail.
    assert response.status_code >= 400, response.text
