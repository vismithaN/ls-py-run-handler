import asyncio
import io
import uuid
from typing import Any, Dict, Optional

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


class Run(BaseModel):
    id: Optional[UUID4] = Field(default_factory=uuid.uuid4)
    trace_id: UUID4
    name: str
    inputs: Dict[str, Any] = {}
    outputs: Dict[str, Any] = {}
    metadata: Dict[str, Any] = {}


async def get_db_conn():
    """Get a database connection."""
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
    """Get an S3 client for MinIO."""
    session = get_s3_session()
    async with session.create_client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
    ) as client:
        yield client


async def prepare_runs(request: Request):
    """Validate and encode incrementally before acquiring storage resources."""
    media_type = request.headers.get("content-type") or "application/json"
    media_type = media_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json" and not (
        media_type.startswith("application/") and media_type.endswith("+json")
    ):
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

    # Prepare the batch for S3 upload
    batch_id = str(uuid.uuid4())

    object_key = f"batches/{batch_id}.json"

    batch = io.BytesIO()
    batch.write(b"[")
    records = []
    run_ids = []
    errors = []
    index = 0
    async for item in json_array_items(request):
        try:
            run = Run.model_validate(item, from_attributes=True)
        except ValidationError as error:
            for detail in error.errors(include_url=False):
                detail["loc"] = ("body", index, *detail["loc"])
                errors.append(detail)
            index += 1
            item = None
            continue
        item = None
        index += 1
        if errors:
            del run
            continue
        if records:
            batch.write(b",")
        batch.write(b"{")
        field_refs = {}
        for field_index, (key, value) in enumerate(run.model_dump().items()):
            if field_index:
                batch.write(b",")
            batch.write(orjson.dumps(key) + b":")
            start = batch.tell()
            encoded = orjson.dumps(value)
            batch.write(encoded)
            if key in ("inputs", "outputs", "metadata"):
                field_refs[key] = (
                    f"s3://{settings.S3_BUCKET_NAME}/{object_key}"
                    f"#{start}:{start + len(encoded)}/{key}"
                )
        batch.write(b"}")
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
    if errors:
        raise RequestValidationError(errors)
    if not records:
        raise HTTPException(status_code=400, detail="No runs provided")
    batch_data = batch.getvalue()
    return object_key, batch_data, records, run_ids


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
    prepared: tuple = Depends(prepare_runs),
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
):
    """Upload one fully validated batch and write its references atomically."""
    object_key, batch_data, records, run_ids = prepared

    # Upload the batch data
    await s3.put_object(
        Bucket=settings.S3_BUCKET_NAME,
        Key=object_key,
        Body=batch_data,
        ContentType="application/json",
    )

    # Send all references together; a failed batch must not leave partial rows.
    async with db.transaction():
        await db.copy_records_to_table(
            "runs",
            records=records,
            columns=("id", "trace_id", "name", "inputs", "outputs", "metadata"),
        )

    # These values are already JSON-compatible; skip FastAPI's conversion walk.
    return ORJSONResponse(
        {"status": "created", "run_ids": run_ids},
        status_code=status.HTTP_201_CREATED,
    )


@router.get("/{run_id}", status_code=status.HTTP_200_OK, response_class=ORJSONResponse)
async def get_run(
    run_id: UUID4,
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
):
    """
    Get a run by its ID.
    """
    # Fetch the run from the PG
    row = await db.fetchrow(
        """
        SELECT id, trace_id, name, inputs, outputs, metadata
        FROM runs
        WHERE id = $1
        """,
        run_id,
    )

    if not row:
        raise HTTPException(status_code=404, detail=f"Run with ID {run_id} not found")

    run_data = dict(row)

    # Function to parse S3 reference
    def parse_s3_ref(ref):
        if not ref or not ref.startswith("s3://"):
            return None, None, None, None

        parts = ref.split("/")
        bucket = parts[2]
        key = "/".join(parts[3:]).split("#")[0]

        if "#" in ref:
            offset_part = ref.split("#")[1]
            if ":" in offset_part and "/" in offset_part:
                offsets, field = offset_part.split("/")
                start_offset, end_offset = map(int, offsets.split(":"))
                return bucket, key, (start_offset, end_offset), field

        return bucket, key, None, None

    async def fetch_range(bucket, key, start, end):
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
        if not bucket or not key or not offsets:
            return {}
        return orjson.loads(await fetch_range(bucket, key, *offsets))

    fields = ("inputs", "outputs", "metadata")
    try:
        refs = [parse_s3_ref(run_data[field]) for field in fields]
        if (
            all(bucket and key and offsets for bucket, key, offsets, _ in refs)
            and len({(bucket, key) for bucket, key, _, _ in refs}) == 1
        ):
            # One range covers the three fields without fetching the full batch.
            bucket, key = refs[0][:2]
            start = min(offsets[0] for _, _, offsets, _ in refs)
            end = max(offsets[1] for _, _, offsets, _ in refs)
            data = await fetch_range(bucket, key, start, end)
            values = [
                orjson.loads(data[offsets[0] - start : offsets[1] - start])
                for _, _, offsets, _ in refs
            ]
        else:
            # Older references may point to different S3 objects.
            values = await asyncio.gather(*(fetch_field(ref) for ref in refs))
    except Exception as e:
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
