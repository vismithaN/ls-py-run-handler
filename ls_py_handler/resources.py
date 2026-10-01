"""Application-scoped PostgreSQL and S3 resources."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import asyncpg
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request

from ls_py_handler.config.settings import settings


@dataclass
class AppResources:
    db_pool: asyncpg.Pool
    s3: Any


async def ensure_bucket(s3: Any) -> None:
    """Create the configured bucket, tolerating only already-existing errors."""

    try:
        await s3.create_bucket(Bucket=settings.S3_BUCKET_NAME)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code not in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
            raise


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own reusable network resources for the full application lifetime."""

    db_pool = await asyncpg.create_pool(
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        min_size=settings.DB_POOL_MIN_SIZE,
        max_size=settings.DB_POOL_MAX_SIZE,
        command_timeout=settings.DB_COMMAND_TIMEOUT_SECONDS,
    )

    session = get_session()
    client_context = session.create_client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=AioConfig(
            connect_timeout=settings.S3_CONNECT_TIMEOUT_SECONDS,
            read_timeout=settings.S3_READ_TIMEOUT_SECONDS,
            max_pool_connections=settings.S3_MAX_CONNECTIONS,
            retries={"max_attempts": settings.S3_MAX_ATTEMPTS, "mode": "standard"},
        ),
    )

    try:
        async with client_context as s3:
            await ensure_bucket(s3)
            app.state.resources = AppResources(db_pool=db_pool, s3=s3)
            yield
    finally:
        await db_pool.close()


async def get_db_conn(request: Request) -> AsyncIterator[asyncpg.Connection]:
    """Borrow a connection from the bounded application pool."""

    resources: AppResources = request.app.state.resources
    try:
        async with resources.db_pool.acquire(
            timeout=settings.DB_POOL_ACQUIRE_TIMEOUT_SECONDS
        ) as connection:
            yield connection
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=503, detail="Run index is temporarily unavailable"
        ) from exc


def get_s3_client(request: Request) -> Any:
    """Return the application-scoped S3 client."""

    resources: AppResources = request.app.state.resources
    return resources.s3
