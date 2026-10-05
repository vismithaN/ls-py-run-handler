"""Parse request JSON arrays incrementally without retaining the decoded list."""

import codecs
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError

DECODE_CHUNK_BYTES = 256 * 1024
READ_AHEAD_MIN_CHARS = 256 * 1024
READ_AHEAD_MAX_CHARS = 512 * 1024


def json_error(message: str, position: int) -> RequestValidationError:
    """Build a JSON-validation exception at the given character offset."""
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


async def text_chunks(request: Request) -> AsyncIterator[str]:
    """Yield request text using incremental UTF-8/16/32 decoding."""
    prefix = bytearray()
    decoder = None
    async for chunk in request.stream():
        # Limit each decoding step even if the server provides a larger chunk.
        for start in range(0, len(chunk), DECODE_CHUNK_BYTES):
            part = memoryview(chunk)[start : start + DECODE_CHUNK_BYTES]
            if decoder is None:
                # Inspect four bytes to distinguish UTF-8/16/32 encodings.
                prefix.extend(part)
                if len(prefix) < 4:
                    continue
                decoder = codecs.getincrementaldecoder(json.detect_encoding(prefix))()
                yield decoder.decode(prefix)
                prefix.clear()
            else:
                # Reuse the decoder so characters split across chunks stay intact.
                yield decoder.decode(part)
    if decoder is None:
        # Handle empty bodies and prefixes too short for the four-byte check.
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
    # Flush the decoder to detect an incomplete final character.
    yield decoder.decode(b"", final=True)


async def json_array_items(request: Request) -> AsyncIterator[Any]:
    """Yield JSON array items; exhaust the iterator to validate the entire body.

    Syntax errors raise validation errors; decoding failures raise HTTP 400.
    Chunk/read-ahead limits do not cap the total buffer or request size.
    """
    chunks = text_chunks(request).__aiter__()
    # Keep unconsumed text; offset tracks its position in the original body.
    buffer = ""
    offset = 0
    finished = False
    decoder = json.JSONDecoder()
    read_hint = 0

    async def read_more() -> bool:
        """Append a text chunk, returning False at EOF."""
        nonlocal buffer, finished
        try:
            buffer += await anext(chunks)
            return True
        except StopAsyncIteration:
            finished = True
            return False
        except (UnicodeError, ValueError, RecursionError) as error:
            raise HTTPException(400, "There was an error parsing the body") from error

    def consume(count: int) -> None:
        """Drop decoded characters and advance the absolute error offset."""
        nonlocal buffer, offset
        buffer = buffer[count:]
        offset += count

    async def skip_space() -> None:
        """Consume JSON whitespace, fetching more text as needed."""
        while True:
            consume(len(buffer) - len(buffer.lstrip(" \t\r\n")))
            if buffer or finished:
                return
            await read_more()

    await skip_space()
    if not buffer:
        raise json_error("Expecting value", offset)
    if buffer[0] != "[":
        # Fully decode non-array bodies only to preserve FastAPI's type errors.
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
    # Consume the opening bracket, then distinguish empty and nonempty arrays.
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
                    # The item may be incomplete; retry after reading more text.
                    if not finished and await read_more():
                        continue
                    raise json_error(error.msg, offset + error.pos) from error
                except (ValueError, RecursionError) as error:
                    raise HTTPException(
                        400, "There was an error parsing the body"
                    ) from error
                # Check the delimiter so partial numbers are not yielded too soon.
                separator = end
                while separator < len(buffer) and buffer[separator] in " \t\r\n":
                    separator += 1
                if separator < len(buffer) and buffer[separator] in ",]":
                    # Use this item's character length as a capped read-ahead hint.
                    read_hint = (
                        min(end, READ_AHEAD_MAX_CHARS)
                        if end > READ_AHEAD_MIN_CHARS
                        else 0
                    )
                    break
                # More bytes may complete a number, whitespace or delimiter.
                del value
                if not finished and await read_more():
                    continue
                raise json_error("Expecting ',' delimiter", offset + separator)
            closing = buffer[separator] == "]"
            # Drop consumed text before handing the item back to the caller.
            consume(separator + 1)
            yield value
            del value
            if closing:
                break
            await skip_space()
            if buffer.startswith("]"):
                # A closing bracket immediately after a comma is invalid.
                raise json_error("Expecting value", offset)
    # After the closing bracket, only whitespace is allowed.
    await skip_space()
    if buffer:
        raise json_error("Extra data", offset)
