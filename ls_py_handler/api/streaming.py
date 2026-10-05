"""Read a JSON array without retaining the complete decoded input list."""

import codecs
import json

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError

CHUNK_BYTES = 256 * 1024


def json_error(message, position):
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
    """Bound decoded text chunks; preserve JSON's UTF-8/16/32 detection."""
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
    """Yield one value at a time, and reject incomplete arrays/trailing JSON."""
    chunks = text_chunks(request).__aiter__()
    buffer = ""
    offset = 0
    finished = False
    decoder = json.JSONDecoder()
    read_hint = 0

    async def read_more():
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
        nonlocal buffer, offset
        buffer = buffer[count:]
        offset += count

    async def skip_space():
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
