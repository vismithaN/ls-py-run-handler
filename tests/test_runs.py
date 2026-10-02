import uuid

import pytest

from ls_py_handler.api.routes.runs import Run
from ls_py_handler.config.settings import settings

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


async def test_create_and_get_run(client):
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
    await assert_roundtrip(client, runs)


async def test_generated_ids(client):
    runs = [
        make_run("First", inputs={"value": 1}),
        make_run("Second", inputs={"value": 2}),
    ]
    for run in runs:
        del run["id"]
    await assert_roundtrip(client, runs)


async def test_large_payload(client):
    run = make_run(
        inputs={"text": "i" * (100 * 1024)},
        outputs={"text": "o" * (100 * 1024)},
        metadata={"text": "m" * (100 * 1024)},
    )
    await assert_roundtrip(client, [run])


async def test_nested_json_and_special_characters(client):
    run = make_run(
        inputs={
            "nested": {"items": [None, True, False, 0, -7, 1.25, "", {"deep": []}]}
        },
        outputs={"text": '🌍 தமிழ் 中文 café "quotes" \\path\nnext\tcolumn'},
        metadata={"tags": ["one", "two"], "details": {"enabled": True}},
    )
    await assert_roundtrip(client, [run])


async def test_empty_and_omitted_fields(client):
    explicit = make_run("Empty", inputs={}, outputs={}, metadata={})
    omitted = make_run("Omitted")
    for field in ("inputs", "outputs", "metadata"):
        del omitted[field]
    await assert_roundtrip(client, [explicit, omitted])


async def test_repeated_field_content(client):
    shared = {"same": [1, "repeat", {"empty": {}}]}
    runs = [
        make_run("Nested", inputs={"wrapper": shared}, outputs=shared, metadata={}),
        make_run("Repeated", inputs=shared, outputs=shared, metadata=shared),
    ]
    await assert_roundtrip(client, runs)


async def test_invalid_requests_and_missing_runs(client, db, s3):
    before_rows = await db.fetchval("SELECT count(*) FROM runs")
    # This isolated suite creates fewer than 1,000 objects; one listing suffices.
    before_objects = await s3.list_objects_v2(Bucket=settings.S3_BUCKET_NAME)

    response = await client.post("/runs", json=[])
    assert response.status_code == 400

    invalid = make_run("Missing trace")
    del invalid["trace_id"]
    response = await client.post("/runs", json=[make_run("Valid"), invalid])
    assert response.status_code == 422

    assert await db.fetchval("SELECT count(*) FROM runs") == before_rows
    after_objects = await s3.list_objects_v2(Bucket=settings.S3_BUCKET_NAME)
    assert {item["Key"] for item in after_objects.get("Contents", [])} == {
        item["Key"] for item in before_objects.get("Contents", [])
    }

    response = await client.get("/runs/not-a-uuid")
    assert response.status_code == 422
    response = await client.get(f"/runs/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_missing_storage_object(client, db, s3):
    run = make_run(inputs={"prompt": "hello"}, outputs={"answer": "world"})
    await assert_roundtrip(client, [run])
    reference = await db.fetchval(
        "SELECT inputs FROM runs WHERE id = $1", uuid.UUID(run["id"])
    )
    # Reference format: s3://bucket/object-key#start:end/field
    key = reference.split("#", 1)[0].split("/", 3)[3]
    await s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)

    response = await client.get(f"/runs/{run['id']}")
    # The baseline may fail: storage errors currently become empty fields + 200.
    # Keep the failure visible rather than changing application code here.
    assert response.status_code >= 400, response.text
