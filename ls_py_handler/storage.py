"""Encoding helpers for versioned run batches stored in object storage."""

from dataclasses import dataclass
from io import BytesIO
from typing import Any, Iterable
from uuid import UUID

import orjson

FORMAT_VERSION = 2


@dataclass(frozen=True)
class RunLocation:
    """The byte range and relational index data for one encoded run."""

    id: UUID
    trace_id: UUID
    name: str
    byte_start: int
    byte_length: int


@dataclass
class EncodedBatch:
    """A seekable NDJSON object plus the locations of its run records."""

    body: BytesIO
    locations: list[RunLocation]
    size: int

    def close(self) -> None:
        self.body.close()


def encode_batch(runs: Iterable[Any], max_bytes: int) -> EncodedBatch:
    """Serialize each run once and record its exact byte range.

    Records are separated by a newline, but the stored ranges exclude the
    newline so every range is independently valid JSON.
    """

    body = BytesIO()
    locations: list[RunLocation] = []

    try:
        for run in runs:
            record = orjson.dumps(run.model_dump())
            byte_start = body.tell()
            byte_length = len(record)

            if byte_start + byte_length + 1 > max_bytes:
                raise ValueError("encoded batch exceeds the configured byte limit")

            body.write(record)
            body.write(b"\n")
            locations.append(
                RunLocation(
                    id=run.id,
                    trace_id=run.trace_id,
                    name=run.name,
                    byte_start=byte_start,
                    byte_length=byte_length,
                )
            )

        size = body.tell()
        body.seek(0)
        return EncodedBatch(body=body, locations=locations, size=size)
    except Exception:
        body.close()
        raise


def decode_run(data: bytes) -> dict:
    """Decode one complete run record from object storage."""

    decoded = orjson.loads(data)
    if not isinstance(decoded, dict):
        raise ValueError("stored run record is not a JSON object")
    return decoded
