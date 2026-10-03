"""Diagnostic-only hooks. Nothing here runs in the headline timing pass."""

import asyncio
from asyncio.selector_events import _SelectorSocketTransport
from collections import Counter, defaultdict
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import urlparse
from weakref import WeakSet

import aiohttp
import asyncpg
from aiobotocore.client import AioBaseClient


class Diagnostics:
    """Count SQL dispatches, S3 HTTP attempts, body bytes and physical TCP opens.

    These hooks are for the pinned asyncpg/aiobotocore versions and Python's
    selector loop. SQL dispatches are not PostgreSQL protocol messages. Prepared
    statement metadata and authentication are deliberately not SQL commands.
    """

    def __init__(self, db_port, s3_url):
        s3 = urlparse(s3_url)
        self.ports = {
            int(db_port): "postgres",
            s3.port or (443 if s3.scheme == "https" else 80): "s3",
        }
        if len(self.ports) != 2:
            raise ValueError("Diagnostic service ports must be different")
        self.phase = "startup"
        self.counts = defaultdict(Counter)
        self.operations = defaultdict(Counter)
        self.active = {}
        # StreamReader in aiohttp 3.11 cannot be weak-referenced. Remove it at EOF.
        self.streams = set()
        self.clients = WeakSet()
        self.stack = ExitStack()

    def begin(self, phase):
        self.phase = phase
        for service in self.ports.values():
            opened = sum(value == service for value in self.active.values())
            self.counts[phase][f"{service}_open_at_start"] = opened
            self.counts[phase][f"{service}_peak_open"] = opened

    def record_sql(self, query):
        # One asyncpg dispatch may contain several SQL statements (pool reset).
        # Keep those explicitly labelled rather than inventing a wire count.
        statements = query.strip().rstrip(";")
        operation = (
            "MULTI"
            if ";" in statements
            else statements.split()[0].upper()
            if statements
            else "EMPTY"
        )
        self.operations[self.phase][f"postgres.{operation}"] += 1
        self.counts[self.phase]["postgres_commands"] += 1

    def __enter__(self):
        if not isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop):
            raise RuntimeError("TCP diagnostic hooks require a selector event loop")

        # fetch/fetchval/fetchrow and parameterized execute share this executor.
        # Count its attempts, including asyncpg's statement-cache retry, rather
        # than overlapping public methods or statement preparation messages.
        original_do_execute = asyncpg.Connection._do_execute

        async def do_execute(connection, query, executor, *args, **kwargs):
            if getattr(executor, "_measurement_collector", None) is self:
                observed = executor
            else:

                async def observed(*executor_args):
                    self.record_sql(query)
                    return await executor(*executor_args)

                observed._measurement_collector = self
            return await original_do_execute(
                connection, query, observed, *args, **kwargs
            )

        original_execute = asyncpg.Connection.execute

        async def execute(connection, query, *args, **kwargs):
            if not args:
                self.record_sql(query)
            return await original_execute(connection, query, *args, **kwargs)

        def copy_wrapper(original):
            async def wrapped(connection, *args, **kwargs):
                self.record_sql("COPY")
                return await original(connection, *args, **kwargs)

            return wrapped

        self.stack.enter_context(
            patch.object(asyncpg.Connection, "_do_execute", do_execute)
        )
        self.stack.enter_context(patch.object(asyncpg.Connection, "execute", execute))
        for method in ("copy_records_to_table", "_copy_in", "_copy_out"):
            self.stack.enter_context(
                patch.object(
                    asyncpg.Connection,
                    method,
                    copy_wrapper(getattr(asyncpg.Connection, method)),
                )
            )

        original_call = AioBaseClient._make_api_call

        def before_send(request, event_name, **kwargs):
            operation = event_name.rsplit(".", 1)[-1]
            self.operations[self.phase][f"s3.{operation}"] += 1
            self.counts[self.phase]["s3_attempts"] += 1
            if operation in {"PutObject", "UploadPart"}:
                length = request.headers.get("Content-Length")
                if length is None:
                    self.counts[self.phase]["upload_bytes_unknown_attempts"] += 1
                else:
                    self.counts[self.phase]["s3_attempted_upload_body_bytes"] += int(
                        length
                    )

        self.before_send = before_send

        async def api_call(client, operation, params):
            if client.meta.service_model.service_name != "s3":
                return await original_call(client, operation, params)

            if client not in self.clients:
                client.meta.events.register("before-send.s3.*", before_send)
                self.clients.add(client)
            # Botocore mutates bytes into a file object while making the call.
            body = params.get("Body", b"")
            size = None
            if isinstance(body, (bytes, bytearray, str)):
                size = len(body.encode() if isinstance(body, str) else body)
            response = await original_call(client, operation, params)
            if operation == "GetObject":
                # __aenter__ returns the raw aiohttp response in this version.
                # Observe its content reader too, not just StreamingBody.read.
                self.streams.add(response["Body"].__wrapped__.content)
            if operation == "PutObject":
                if size is not None:
                    self.counts[self.phase]["s3_stored_object_bytes"] += size
                else:
                    self.counts[self.phase]["stored_bytes_unknown_objects"] += 1
            return response

        original_read = aiohttp.StreamReader.read

        async def read(stream, *args, **kwargs):
            data = await original_read(stream, *args, **kwargs)
            if stream in self.streams:
                self.counts[self.phase]["s3_consumed_body_bytes"] += len(data)
                if stream.at_eof():
                    self.streams.discard(stream)
            return data

        original_connect = asyncio.BaseEventLoop.create_connection

        async def connect(loop, *args, **kwargs):
            transport, protocol = await original_connect(loop, *args, **kwargs)
            peer = transport.get_extra_info("peername")
            if peer and peer[1] in self.ports:
                service = self.ports[peer[1]]
                # SSL application transports wrap the same physical socket.
                ssl_protocol = getattr(transport, "_ssl_protocol", None)
                physical = ssl_protocol._transport if ssl_protocol else transport
                self.active[physical] = service
                self.counts[self.phase][f"{service}_connections_created"] += 1
                opened = sum(value == service for value in self.active.values())
                key = f"{service}_peak_open"
                self.counts[self.phase][key] = max(self.counts[self.phase][key], opened)
            return transport, protocol

        original_close = _SelectorSocketTransport._call_connection_lost

        def close(transport, *args, **kwargs):
            try:
                return original_close(transport, *args, **kwargs)
            finally:
                service = self.active.pop(transport, None)
                if service:
                    self.counts[self.phase][f"{service}_connections_closed"] += 1

        for owner, name, wrapper in (
            (AioBaseClient, "_make_api_call", api_call),
            (aiohttp.StreamReader, "read", read),
            (asyncio.BaseEventLoop, "create_connection", connect),
            (_SelectorSocketTransport, "_call_connection_lost", close),
        ):
            self.stack.enter_context(patch.object(owner, name, wrapper))
        return self

    def __exit__(self, *exc):
        for client in self.clients:
            client.meta.events.unregister("before-send.s3.*", self.before_send)
        self.stack.close()

    def snapshot(self):
        return dict(self.counts[self.phase]), dict(self.operations[self.phase])

    def result(self):
        keys = (
            "postgres_commands",
            "s3_attempts",
            "s3_attempted_upload_body_bytes",
            "s3_stored_object_bytes",
            "s3_consumed_body_bytes",
            "upload_bytes_unknown_attempts",
            "stored_bytes_unknown_objects",
        )
        phases = {}
        for phase, counts in self.counts.items():
            # A measured zero is distinct from an absent collector in other passes.
            values = {key: counts[key] for key in keys}
            for service in self.ports.values():
                for metric in (
                    "connections_created",
                    "connections_closed",
                    "open_at_start",
                    "peak_open",
                ):
                    key = f"{service}_{metric}"
                    values[key] = counts[key]
            phases[phase] = {
                "counts": values,
                "operations": dict(self.operations[phase]),
            }
        return {
            "coverage": (
                "selector TCP open/physical-close events; "
                "S3 before-send includes retries"
            ),
            "sql_scope": (
                "asyncpg Connection execution attempts including driver retries; "
                "COPY API attempts; MULTI/executemany are one dispatch per attempt; "
                "not wire messages or individual rows; PreparedStatement APIs excluded"
            ),
            "byte_scope": (
                "upload attempted bodies; successful PUT object sizes; "
                "GET bytes consumed by application; excludes headers/TCP framing"
            ),
            "phases": phases,
        }
