"""Decode a request's JSON array incrementally for batch ingestion.

The parser yields individual JSON values to the route for model validation and
encoding, avoiding a complete decoded input list. It handles text encoding,
array delimiters, and request-validation errors without accessing storage.
Chunked decoding does not impose a maximum request size or total memory limit.
"""

import codecs
import json

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError

CHUNK_BYTES = 256 * 1024


def json_error(message, position):
    """Build a FastAPI JSON-validation exception at a decoded character offset.

    Args:
        message: Description of the JSON syntax error.
        position: Absolute character offset in the decoded request body.

    Returns:
        A RequestValidationError for the caller to raise, using the same error
        structure as FastAPI's JSON-body validation.
    """
    return RequestValidationError(
        [
            {
                "type": "json_invalid",
                "loc": ("body", position),
                "msg": "JSON decode error",
                "input": {},
                "ctx": {"error": message},
            }
        ]
    )


async def text_chunks(request: Request):
    """Yield decoded text while preserving characters split across byte chunks.

    Detect UTF-8, UTF-16, or UTF-32 from the initial bytes and use an incremental
    decoder to handle partial characters. Each incoming ASGI chunk is processed
    in pieces of at most CHUNK_BYTES bytes; the final decode checks for incomplete
    characters. The upstream server may already hold a larger incoming chunk.

    Args:
        request: Request whose body stream has not yet been consumed.

    Yields:
        Decoded text fragments, which may be empty while characters are buffered.

    Raises:
        RequestValidationError: The request body is empty.
        UnicodeError: Body bytes contain malformed or incomplete encoded text.
    """
    prefix = bytearray()
    decoder = None
    async for chunk in request.stream():
        for start in range(0, len(chunk), CHUNK_BYTES):
            part = memoryview(chunk)[start : start + CHUNK_BYTES]
            if decoder is None:
                prefix.extend(part)
                if len(prefix) < 4:
                    continue
                decoder = codecs.getincrementaldecoder(json.detect_encoding(prefix))()
                yield decoder.decode(prefix)
                prefix.clear()
            else:
                yield decoder.decode(part)
    if decoder is None:
        if not prefix:
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
        decoder = codecs.getincrementaldecoder(json.detect_encoding(prefix))()
        yield decoder.decode(prefix)
    yield decoder.decode(b"", final=True)


async def json_array_items(request: Request):
    """Yield JSON array items without constructing the full decoded input list.

    Accumulate text until an item and its following delimiter can be decoded,
    then discard the consumed text and yield that item. For large items, use the
    previous item's decoded length as a read-ahead hint, capped at 2 * CHUNK_BYTES
    characters, to reduce repeated parsing attempts. This hint is not a size limit:
    a single item can still require a large buffer.

    The caller validates each item's Run schema and must exhaust this generator
    before writing to storage: a later item, missing closing bracket, or trailing
    content can still invalidate a batch after earlier items have been yielded.
    Non-array bodies use a full-body error path to preserve validation behavior.

    Args:
        request: Request containing the JSON array to decode.

    Yields:
        Individual decoded JSON values in their original array order.

    Raises:
        RequestValidationError: Missing body, invalid JSON, or a non-array root.
        HTTPException: Text decoding or parser limits prevent body parsing (400).
    """
    chunks = text_chunks(request).__aiter__()
    buffer = ""
    offset = 0
    finished = False
    decoder = json.JSONDecoder()
    read_hint = 0

    async def read_more():
        """Append the next text chunk; return False and mark EOF when exhausted."""
        nonlocal buffer, finished
        try:
            buffer += await anext(chunks)
            return True
        except StopAsyncIteration:
            finished = True
            return False
        except (UnicodeError, ValueError, RecursionError) as error:
            raise HTTPException(400, "There was an error parsing the body") from error

    def consume(count):
        """Drop decoded characters and advance the absolute error offset."""
        nonlocal buffer, offset
        buffer = buffer[count:]
        offset += count

    async def skip_space():
        """Discard JSON whitespace, reading more text when the buffer is empty."""
        while True:
            consume(len(buffer) - len(buffer.lstrip(" \t\r\n")))
            if buffer or finished:
                return
            await read_more()

    await skip_space()
    if not buffer:
        raise json_error("Expecting value", offset)
    if buffer[0] != "[":
        # Error-only path: preserve the original root-type validation response.
        while await read_more():
            pass
        try:
            value = json.loads(buffer)
        except json.JSONDecodeError as error:
            raise json_error(error.msg, offset + error.pos) from error
        except (ValueError, RecursionError) as error:
            raise HTTPException(400, "There was an error parsing the body") from error
        if value is None:
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
                    "input": value,
                }
            ]
        )
    consume(1)
    await skip_space()
    if buffer.startswith("]"):
        consume(1)
    else:
        while True:
            # Large, similar-sized runs need similar amounts of input. Read ahead
            # before retrying, without changing the small-run path.
            while len(buffer) < read_hint and not finished:
                if not await read_more():
                    break
            while True:
                try:
                    value, end = decoder.raw_decode(buffer)
                except json.JSONDecodeError as error:
                    if not finished and await read_more():
                        continue
                    raise json_error(error.msg, offset + error.pos) from error
                except (ValueError, RecursionError) as error:
                    raise HTTPException(
                        400, "There was an error parsing the body"
                    ) from error
                separator = end
                while separator < len(buffer) and buffer[separator] in " \t\r\n":
                    separator += 1
                if separator < len(buffer) and buffer[separator] in ",]":
                    read_hint = min(end, 2 * CHUNK_BYTES) if end > CHUNK_BYTES else 0
                    break
                # More bytes may complete a number, whitespace or delimiter.
                del value
                if not finished and await read_more():
                    continue
                raise json_error("Expecting ',' delimiter", offset + separator)
            closing = buffer[separator] == "]"
            consume(separator + 1)
            yield value
            del value
            if closing:
                break
            await skip_space()
            if buffer.startswith("]"):
                raise json_error("Expecting value", offset)
    await skip_space()
    if buffer:
        raise json_error("Extra data", offset)
