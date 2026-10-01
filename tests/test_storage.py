import uuid

import pytest

from ls_py_handler.api.routes.runs import Run
from ls_py_handler.storage import decode_run, encode_batch


def test_encode_batch_records_independent_exact_json_ranges():
    shared = {"duplicate": "same", "unicode": "雪 🌨️", "escaped": "a\nb\\c"}
    runs = [
        Run(trace_id=uuid.uuid4(), name="first", inputs=shared),
        Run(trace_id=uuid.uuid4(), name="second", outputs=shared),
    ]

    batch = encode_batch(runs, max_bytes=10_000)
    try:
        encoded = batch.body.getvalue()
        assert batch.size == len(encoded)
        assert len(batch.locations) == 2

        for run, location in zip(runs, batch.locations, strict=True):
            record = encoded[
                location.byte_start : location.byte_start + location.byte_length
            ]
            assert decode_run(record) == run.model_dump(mode="json")
    finally:
        batch.close()


def test_encode_batch_enforces_total_encoded_size_including_separator():
    run = Run(trace_id=uuid.uuid4(), name="size limit")
    exact = encode_batch([run], max_bytes=10_000)
    exact_size = exact.size
    exact.close()

    with pytest.raises(ValueError, match="encoded batch exceeds"):
        encode_batch([run], max_bytes=exact_size - 1)


def test_decode_run_rejects_non_object_json():
    with pytest.raises(ValueError, match="not a JSON object"):
        decode_run(b"[]")
