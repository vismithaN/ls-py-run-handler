"""JSON request logs without buffering bodies or changing streaming behavior."""

import json
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)
request_id = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    """Expose safe request and storage context; omit payloads and credentials."""

    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        correlation = getattr(record, "request_id", None) or request_id.get()
        if correlation:
            event["request_id"] = correlation
        for field in (
            "operation",
            "method",
            "route",
            "status_code",
            "duration_ms",
            "object_key",
            "run_count",
            "run_id",
            "error_type",
        ):
            if hasattr(record, field):
                event[field] = getattr(record, field)
        return json.dumps(event, separators=(",", ":"))


class RequestLoggingMiddleware:
    """Log completion/status and add X-Request-ID to handled responses.

    Log route templates rather than user-supplied paths. Pass request and response
    bodies through unchanged; elapsed time ends when the response finishes.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        correlation = uuid.uuid4().hex
        token = request_id.set(correlation)
        started = time.perf_counter()
        status_code = 500

        async def send_response(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (b"x-request-id", correlation.encode("ascii")),
                    ],
                }
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        finally:
            try:
                logger.info(
                    "Request completed",
                    extra={
                        "operation": "http_request",
                        "request_id": correlation,
                        "method": scope["method"],
                        "route": getattr(scope.get("route"), "path", "unmatched"),
                        "status_code": status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    },
                )
            finally:
                request_id.reset(token)
