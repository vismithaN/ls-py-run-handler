import asyncio
import io
import logging
import uuid
from typing import Any, Dict, NamedTuple, Optional

import asyncpg
import orjson
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from pydantic import UUID4, BaseModel, Field, ValidationError

from ls_py_handler.api.streaming import json_array_items
from ls_py_handler.config.settings import settings
from ls_py_handler.s3 import get_s3_session

router = APIRouter(prefix="/runs", tags=["runs"])
logger = logging.getLogger(__name__)


class Run(BaseModel):
    id: Optional[UUID4] = Field(default_factory=uuid.uuid4)
    trace_id: UUID4
    name: str
    inputs: Dict[str, Any] = {}
    outputs: Dict[str, Any] = {}
    metadata: Dict[str, Any] = {}


class PreparedBatch(NamedTuple):
    """Validated upload bytes, database reference rows, and response IDs."""

    object_key: str
    batch_data: bytes
    records: list[tuple]
    run_ids: list[str]


async def get_db_conn():
    """Yield a request-scoped database connection and close it afterwards."""
    conn = await asyncpg.connect(
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        host=settings.DB_HOST,
        port=settings.DB_PORT,
    )
    try:
        yield conn
    finally:
        await conn.close()


async def get_s3_client():
    """Yield a request-scoped S3 client using the cached SDK session."""
    session = get_s3_session()
    async with session.create_client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
    ) as client:
        yield client


async def prepare_runs(request: Request) -> PreparedBatch:
    """Validate the complete request before acquiring storage resources.

    Return upload bytes, COPY rows and IDs; the outgoing batch stays in memory.
    Invalid input raises an HTTP/validation error without writing to storage.
    """
    # Accept JSON media types, including vendor types such as application/*+json.
    media_type = request.headers.get("content-type") or "application/json"
    media_type = media_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json" and not (
        media_type.startswith("application/") and media_type.endswith("+json")
    ):
        # Preserve distinct validation errors for missing and non-JSON bodies.
        body = await request.body()
        if not body:
            raise RequestValidationError(
                [
                    {
                        "type": "missing",
                        "loc": ("body",),
                        "msg": "Field required",
                        "input": None,
                    }
                ]
            )
        raise RequestValidationError(
            [
                {
                    "type": "list_type",
                    "loc": ("body",),
                    "msg": "Input should be a valid list",
                    "input": body,
                }
            ]
        )

    # One S3 object holds the batch; database rows reference its field byte ranges.
    batch_id = str(uuid.uuid4())

    object_key = f"batches/{batch_id}.json"

    batch = io.BytesIO()
    batch.write(b"[")
    records = []
    run_ids = []
    errors = []
    index = 0
    # Validate and encode one run at a time instead of building a full model list.
    async for item in json_array_items(request):
        try:
            run = Run.model_validate(item, from_attributes=True)
        except ValidationError as error:
            # Include the original array index in each field's validation error.
            for detail in error.errors(include_url=False):
                detail["loc"] = ("body", index, *detail["loc"])
                errors.append(detail)
            index += 1
            item = None
            continue
        item = None
        index += 1
        if errors:
            # Collect further validation errors without encoding a rejected batch.
            del run
            continue
        if records:
            batch.write(b",")
        batch.write(b"{")
        field_refs = {}
        # Record offsets while encoding, so repeated field values stay distinct.
        for field_index, (key, value) in enumerate(run.model_dump().items()):
            if field_index:
                batch.write(b",")
            batch.write(orjson.dumps(key) + b":")
            start = batch.tell()
            encoded = orjson.dumps(value)
            batch.write(encoded)
            if key in ("inputs", "outputs", "metadata"):
                # Store the value's byte range with an exclusive end offset.
                field_refs[key] = (
                    f"s3://{settings.S3_BUCKET_NAME}/{object_key}"
                    f"#{start}:{start + len(encoded)}/{key}"
                )
        batch.write(b"}")
        # Keep small COPY rows: identifiers, name, and references to large fields.
        records.append(
            (
                run.id,
                run.trace_id,
                run.name,
                field_refs["inputs"],
                field_refs["outputs"],
                field_refs["metadata"],
            )
        )
        run_ids.append(str(run.id))
        # Keep only encoded bytes and small DB records, not all input models.
        del run, value, encoded, field_refs
    batch.write(b"]")
    # Reject invalid or empty batches before the endpoint can write to storage.
    if errors:
        raise RequestValidationError(errors)
    if not records:
        raise HTTPException(status_code=400, detail="No runs provided")
    batch_data = batch.getvalue()
    return PreparedBatch(object_key, batch_data, records, run_ids)


# Streaming parsing hides the body type from FastAPI; declare its API schema.
@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={
        422: {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                }
            },
        }
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "array",
                        "title": "Runs",
                        "items": Run.model_json_schema(),
                    }
                }
            },
        }
    },
)
async def create_runs(
    prepared: PreparedBatch = Depends(prepare_runs),
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
):
    """Upload a validated batch, then write all references in one DB transaction.

    Only the database writes are atomic; a DB failure can leave the S3 object.
    """

    # Upload the batch data
    try:
        await s3.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=prepared.object_key,
            Body=prepared.batch_data,
            ContentType="application/json",
        )
    except Exception as error:
        logger.error(
            "Failed to upload run batch (%s)",
            type(error).__name__,
            extra={
                "operation": "upload_batch",
                "object_key": prepared.object_key,
                "run_count": len(prepared.records),
                "error_type": type(error).__name__,
            },
        )
        raise

    # Send all references together; a failed batch must not leave partial rows.
    try:
        async with db.transaction():
            await db.copy_records_to_table(
                "runs",
                records=prepared.records,
                columns=("id", "trace_id", "name", "inputs", "outputs", "metadata"),
            )
    except Exception as error:
        logger.error(
            "Failed to store run batch references (%s)",
            type(error).__name__,
            extra={
                "operation": "write_batch_references",
                "object_key": prepared.object_key,
                "run_count": len(prepared.records),
                "error_type": type(error).__name__,
            },
        )
        raise

    # These values are already JSON-compatible; skip FastAPI's conversion walk.
    return ORJSONResponse(
        {"status": "created", "run_ids": prepared.run_ids},
        status_code=status.HTTP_201_CREATED,
    )


@router.get("/{run_id}", status_code=status.HTTP_200_OK, response_class=ORJSONResponse)
async def get_run(
    run_id: UUID4,
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
):
    """Retrieve a run using one S3 range when all fields share an object.

    Older different-object references are read concurrently. Return 404 for an
    unknown run and 503 for invalid references or unreadable stored data.
    """
    # Fetch run metadata and S3 references, rather than the large JSON fields.
    try:
        row = await db.fetchrow(
            """
            SELECT id, trace_id, name, inputs, outputs, metadata
            FROM runs
            WHERE id = $1
            """,
            run_id,
        )
    except Exception as error:
        logger.error(
            "Failed to fetch run references (%s)",
            type(error).__name__,
            extra={
                "operation": "fetch_run_references",
                "run_id": str(run_id),
                "error_type": type(error).__name__,
            },
        )
        raise

    if not row:
        raise HTTPException(status_code=404, detail=f"Run with ID {run_id} not found")

    run_data = dict(row)

    # References encode the bucket, object key, byte range, and field name.
    def parse_s3_ref(ref, expected_field):
        if not isinstance(ref, str) or not ref.startswith("s3://"):
            raise ValueError("Invalid S3 reference")
        location, fragment = ref[5:].split("#", 1)
        bucket, key = location.split("/", 1)
        offsets, field = fragment.rsplit("/", 1)
        start_offset, end_offset = map(int, offsets.split(":"))
        if (
            not bucket
            or not key
            or start_offset < 0
            or end_offset <= start_offset
            or field != expected_field
        ):
            raise ValueError("Invalid S3 reference")
        return bucket, key, (start_offset, end_offset), field

    async def fetch_range(bucket, key, start, end):
        # Stored end offsets are exclusive; S3 expects an inclusive end byte.
        response = await s3.get_object(
            Bucket=bucket, Key=key, Range=f"bytes={start}-{end - 1}"
        )
        async with response["Body"] as stream:
            data = await stream.read()
        if len(data) != end - start:
            raise ValueError("Incomplete S3 range response")
        return data

    async def fetch_field(ref):
        bucket, key, offsets, _ = ref
        return orjson.loads(await fetch_range(bucket, key, *offsets))

    fields = ("inputs", "outputs", "metadata")
    try:
        refs = [parse_s3_ref(run_data[field], field) for field in fields]
        if len({(bucket, key) for bucket, key, _, _ in refs}) == 1:
            # One range covers the three fields without fetching the full batch.
            bucket, key = refs[0][:2]
            start = min(offsets[0] for _, _, offsets, _ in refs)
            end = max(offsets[1] for _, _, offsets, _ in refs)
            data = await fetch_range(bucket, key, start, end)
            # Rebase each batch offset onto the fetched span before decoding.
            values = [
                orjson.loads(data[offsets[0] - start : offsets[1] - start])
                for _, _, offsets, _ in refs
            ]
        else:
            # Older references may point to different S3 objects.
            values = await asyncio.gather(*(fetch_field(ref) for ref in refs))
    except Exception as e:
        logger.error(
            "Failed to retrieve stored run data (%s)",
            type(e).__name__,
            extra={
                "operation": "read_run_data",
                "run_id": str(run_id),
                "error_type": type(e).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stored run data is unavailable",
        ) from e

    inputs, outputs, metadata = values

    # S3 fields are already decoded JSON values; skip another conversion walk.
    return ORJSONResponse(
        {
            "id": str(run_data["id"]),
            "trace_id": str(run_data["trace_id"]),
            "name": run_data["name"],
            "inputs": inputs,
            "outputs": outputs,
            "metadata": metadata,
        }
    )
