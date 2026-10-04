import asyncio
import io
import json
import uuid
from typing import Any, Dict, List, Optional

import asyncpg
import orjson
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from pydantic import UUID4, BaseModel, Field, TypeAdapter, ValidationError

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


run_batch_adapter = TypeAdapter(List[Run])


async def parse_runs(request: Request) -> List[Run]:
    """Parse and validate raw JSON together, without a separate decoded payload."""
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
    media_type = request.headers.get("content-type", "application/json")
    media_type = media_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json" and not (
        media_type.startswith("application/") and media_type.endswith("+json")
    ):
        raise RequestValidationError(
            [
                {
                    "type": "list_type",
                    "loc": ("body",),
                    "msg": "Input should be a valid list",
                    "input": body.decode(errors="replace"),
                }
            ]
        )
    try:
        return run_batch_adapter.validate_json(body)
    except ValidationError as error:
        errors = error.errors(include_url=False)
        if errors[0]["type"] == "json_invalid":
            # Keep the old parser for uncommon encodings/deep JSON it accepts,
            # and retain its syntax-error locations. Normal JSON stays one-pass.
            try:
                return run_batch_adapter.validate_python(json.loads(body))
            except json.JSONDecodeError as syntax_error:
                raise RequestValidationError(
                    [
                        {
                            "type": "json_invalid",
                            "loc": ("body", syntax_error.pos),
                            "msg": "JSON decode error",
                            "input": {},
                            "ctx": {"error": syntax_error.msg},
                        }
                    ]
                ) from syntax_error
            except ValidationError as legacy_error:
                errors = legacy_error.errors(include_url=False)
            except (UnicodeDecodeError, ValueError, RecursionError) as decode_error:
                raise HTTPException(
                    400, "There was an error parsing the body"
                ) from decode_error
        for item in errors:
            item["loc"] = ("body", *item["loc"])
        raise RequestValidationError(errors) from error


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


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
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
        },
        "responses": {
            "422": {
                "description": "Validation Error",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                    }
                },
            }
        },
    },
)
async def create_runs(
    runs: List[Run] = Depends(parse_runs),
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
):
    """
    Create new runs in batch.

    Takes a JSON array of Run objects, uploads them to MinIO,
    and stores references to certain fields in PostgreSQL.
    """
    if not runs:
        raise HTTPException(status_code=400, detail="No runs provided")

    # Prepare the batch for S3 upload
    batch_id = str(uuid.uuid4())

    object_key = f"batches/{batch_id}.json"

    batch = io.BytesIO()
    batch.write(b"[")
    run_refs = []
    for index, run in enumerate(runs):
        if index:
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
        run_refs.append(field_refs)
    batch.write(b"]")
    batch_data = batch.getvalue()

    # Upload the batch data
    await s3.put_object(
        Bucket=settings.S3_BUCKET_NAME,
        Key=object_key,
        Body=batch_data,
        ContentType="application/json",
    )

    # Send all references together; a failed batch must not leave partial rows.
    records = (
        (
            run.id,
            run.trace_id,
            run.name,
            field_refs["inputs"],
            field_refs["outputs"],
            field_refs["metadata"],
        )
        for run, field_refs in zip(runs, run_refs, strict=True)
    )
    async with db.transaction():
        await db.copy_records_to_table(
            "runs",
            records=records,
            columns=("id", "trace_id", "name", "inputs", "outputs", "metadata"),
        )

    return {"status": "created", "run_ids": [str(run.id) for run in runs]}


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
