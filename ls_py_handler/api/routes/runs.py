import asyncio
import time
import uuid
from typing import Any, Dict, List

import asyncpg
import orjson
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import UUID4, BaseModel, Field, ValidationError

from ls_py_handler.config.settings import settings
from ls_py_handler.observability import log_event
from ls_py_handler.resources import get_db_conn, get_s3_client
from ls_py_handler.storage import FORMAT_VERSION, decode_run, encode_batch

router = APIRouter(prefix="/runs", tags=["runs"])


class Run(BaseModel):
    id: UUID4 = Field(default_factory=uuid.uuid4)
    trace_id: UUID4
    name: str
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


async def _delete_uploaded_batch(s3: Any, object_key: str, batch_id: str) -> None:
    """Best-effort compensation when the database transaction fails."""

    try:
        await s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=object_key)
        log_event("orphan_batch_removed", batch_id=batch_id, object_key=object_key)
    except Exception as exc:  # Cleanup must not hide the original database failure.
        log_event(
            "orphan_batch_cleanup_failed",
            batch_id=batch_id,
            object_key=object_key,
            error_type=type(exc).__name__,
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_runs(
    request: Request,
    runs: List[Run],
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
):
    """Persist one immutable batch object and bulk-index all of its runs."""

    if not runs:
        raise HTTPException(status_code=400, detail="No runs provided")
    if len(runs) > settings.MAX_BATCH_RUNS:
        raise HTTPException(
            status_code=413,
            detail=f"Batch exceeds the maximum of {settings.MAX_BATCH_RUNS} runs",
        )

    batch_id = str(uuid.uuid4())
    object_key = f"batches/{batch_id}.ndjson"
    started_at = time.perf_counter()

    try:
        batch = encode_batch(runs, settings.MAX_BATCH_BYTES)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    encoded_at = time.perf_counter()
    try:
        try:
            await s3.put_object(
                Bucket=settings.S3_BUCKET_NAME,
                Key=object_key,
                Body=batch.body,
                ContentLength=batch.size,
                ContentType="application/x-ndjson",
                Metadata={"format-version": str(FORMAT_VERSION)},
            )
        except (BotoCoreError, ClientError) as exc:
            log_event(
                "run_batch_upload_failed",
                request_id=request.state.request_id,
                batch_id=batch_id,
                run_count=len(runs),
                batch_bytes=batch.size,
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503, detail="Run storage is temporarily unavailable"
            ) from exc

        uploaded_at = time.perf_counter()
        records = [
            (
                location.id,
                location.trace_id,
                location.name,
                uuid.UUID(batch_id),
                object_key,
                location.byte_start,
                location.byte_length,
                FORMAT_VERSION,
            )
            for location in batch.locations
        ]

        try:
            async with db.transaction():
                await db.copy_records_to_table(
                    "runs",
                    records=records,
                    columns=(
                        "id",
                        "trace_id",
                        "name",
                        "batch_id",
                        "object_key",
                        "byte_start",
                        "byte_length",
                        "format_version",
                    ),
                )
        except asyncpg.UniqueViolationError as exc:
            await _delete_uploaded_batch(s3, object_key, batch_id)
            raise HTTPException(
                status_code=409, detail="One or more run IDs already exist"
            ) from exc
        except asyncpg.PostgresError as exc:
            await _delete_uploaded_batch(s3, object_key, batch_id)
            log_event(
                "run_batch_index_failed",
                request_id=request.state.request_id,
                batch_id=batch_id,
                run_count=len(runs),
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503, detail="Run index is temporarily unavailable"
            ) from exc

        indexed_at = time.perf_counter()
        log_event(
            "run_batch_created",
            request_id=request.state.request_id,
            batch_id=batch_id,
            run_count=len(runs),
            batch_bytes=batch.size,
            encode_ms=round((encoded_at - started_at) * 1_000, 3),
            s3_ms=round((uploaded_at - encoded_at) * 1_000, 3),
            db_ms=round((indexed_at - uploaded_at) * 1_000, 3),
            total_ms=round((indexed_at - started_at) * 1_000, 3),
        )
        return {
            "status": "created",
            "run_ids": [str(location.id) for location in batch.locations],
        }
    finally:
        batch.close()


def _parse_legacy_s3_ref(ref: str) -> tuple[str, str, tuple[int, int]] | None:
    """Parse the starter implementation's version-1 S3 reference."""

    if not ref or not ref.startswith("s3://") or "#" not in ref:
        return None

    path, fragment = ref[5:].split("#", 1)
    bucket, path_separator, key = path.partition("/")
    offsets, field_separator, _field = fragment.partition("/")
    if (
        not bucket
        or not path_separator
        or not key
        or not field_separator
        or ":" not in offsets
    ):
        return None

    start, end = offsets.split(":", 1)
    try:
        return bucket, key, (int(start), int(end))
    except ValueError:
        return None


async def _read_s3_range(
    s3: Any,
    bucket: str,
    key: str,
    byte_start: int,
    byte_length: int,
    run_id: UUID4,
) -> bytes:
    byte_end = byte_start + byte_length - 1
    try:
        response = await s3.get_object(
            Bucket=bucket,
            Key=key,
            Range=f"bytes={byte_start}-{byte_end}",
        )
        async with response["Body"] as stream:
            return await stream.read()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"NoSuchKey", "InvalidRange", "NoSuchBucket"}:
            log_event(
                "run_storage_integrity_error",
                run_id=str(run_id),
                object_key=key,
                error_code=error_code,
            )
            raise HTTPException(
                status_code=500, detail="Stored run data is unavailable"
            ) from exc
        raise HTTPException(
            status_code=503, detail="Run storage is temporarily unavailable"
        ) from exc
    except BotoCoreError as exc:
        raise HTTPException(
            status_code=503, detail="Run storage is temporarily unavailable"
        ) from exc


async def _read_legacy_fields(s3: Any, row: dict, run_id: UUID4) -> tuple:
    async def fetch(ref: str) -> dict:
        parsed = _parse_legacy_s3_ref(ref)
        if parsed is None:
            raise HTTPException(
                status_code=500, detail="Stored run reference is invalid"
            )
        bucket, key, (start, end) = parsed
        data = await _read_s3_range(s3, bucket, key, start, end - start, run_id)
        try:
            decoded = orjson.loads(data)
        except orjson.JSONDecodeError as exc:
            raise HTTPException(
                status_code=500, detail="Stored run data is corrupt"
            ) from exc
        if not isinstance(decoded, dict):
            raise HTTPException(status_code=500, detail="Stored run data is corrupt")
        return decoded

    return await asyncio.gather(
        fetch(row["inputs"]), fetch(row["outputs"]), fetch(row["metadata"])
    )


@router.get("/{run_id}", status_code=status.HTTP_200_OK)
async def get_run(
    request: Request,
    run_id: UUID4,
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
):
    """Retrieve a run with one index lookup and one version-2 range read."""

    started_at = time.perf_counter()
    try:
        row = await db.fetchrow(
            """
            SELECT id, trace_id, name, inputs, outputs, metadata,
                   object_key, byte_start, byte_length, format_version
            FROM runs
            WHERE id = $1
            """,
            run_id,
        )
    except asyncpg.PostgresError as exc:
        log_event(
            "run_index_read_failed",
            request_id=request.state.request_id,
            run_id=str(run_id),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail="Run index is temporarily unavailable"
        ) from exc
    selected_at = time.perf_counter()

    if not row:
        raise HTTPException(status_code=404, detail=f"Run with ID {run_id} not found")

    run_data = dict(row)
    if run_data["format_version"] == FORMAT_VERSION:
        data = await _read_s3_range(
            s3,
            settings.S3_BUCKET_NAME,
            run_data["object_key"],
            run_data["byte_start"],
            run_data["byte_length"],
            run_id,
        )
        fetched_at = time.perf_counter()
        try:
            decoded = decode_run(data)
            validated = Run.model_validate(decoded)
        except (orjson.JSONDecodeError, ValidationError, ValueError) as exc:
            log_event(
                "run_storage_decode_error",
                request_id=request.state.request_id,
                run_id=str(run_id),
                object_key=run_data["object_key"],
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=500, detail="Stored run data is corrupt"
            ) from exc

        if validated.id != run_id:
            log_event(
                "run_storage_id_mismatch",
                request_id=request.state.request_id,
                run_id=str(run_id),
                stored_run_id=str(validated.id),
            )
            raise HTTPException(status_code=500, detail="Stored run data is corrupt")

        decoded_at = time.perf_counter()
        log_event(
            "run_retrieved",
            request_id=request.state.request_id,
            run_id=str(run_id),
            format_version=FORMAT_VERSION,
            bytes_read=len(data),
            db_ms=round((selected_at - started_at) * 1_000, 3),
            s3_ms=round((fetched_at - selected_at) * 1_000, 3),
            decode_ms=round((decoded_at - fetched_at) * 1_000, 3),
            total_ms=round((decoded_at - started_at) * 1_000, 3),
        )
        return validated.model_dump(mode="json")

    inputs, outputs, metadata = await _read_legacy_fields(s3, run_data, run_id)
    return {
        "id": str(run_data["id"]),
        "trace_id": str(run_data["trace_id"]),
        "name": run_data["name"],
        "inputs": inputs,
        "outputs": outputs,
        "metadata": metadata,
    }
