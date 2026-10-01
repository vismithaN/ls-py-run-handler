"""Small structured-logging helpers that never include run payloads."""

import logging
from typing import Any

import orjson

logger = logging.getLogger("ls_py_handler")


def log_event(event: str, **fields: Any) -> None:
    """Emit a JSON log event suitable for ingestion by production log systems."""

    payload = {"event": event, **fields}
    logger.info(orjson.dumps(payload).decode("utf-8"))
