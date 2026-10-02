"""Real-service tests isolated from existing databases and S3 buckets."""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import psycopg2
import pytest
import pytest_asyncio
from aiobotocore.session import get_session
from botocore.config import Config
from httpx import ASGITransport, AsyncClient
from psycopg2 import sql

from ls_py_handler.config.settings import settings
from ls_py_handler.main import app


def s3_client():
    return get_session().create_client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=Config(connect_timeout=5, read_timeout=5, retries={"max_attempts": 0}),
    )


async def prepare_bucket():
    async with s3_client() as s3:
        await s3.create_bucket(Bucket=settings.S3_BUCKET_NAME)


async def remove_bucket():
    # Only the uniquely named bucket created by isolated_storage is in scope.
    async with s3_client() as s3:
        paginator = s3.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=settings.S3_BUCKET_NAME):
            for item in page.get("Contents", []):
                await s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=item["Key"])
        await s3.delete_bucket(Bucket=settings.S3_BUCKET_NAME)


@pytest.fixture(scope="session")
def isolated_storage():
    if os.environ.get("RUN_HANDLER_ENV") != "test":
        pytest.fail("Run correctness tests with RUN_HANDLER_ENV=test")
    if settings.DB_HOST not in {"localhost", "127.0.0.1", "::1"} or urlparse(
        settings.S3_ENDPOINT_URL
    ).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("Correctness fixtures require local PostgreSQL and MinIO")

    suffix = uuid.uuid4().hex
    database = f"correctness_{suffix}"
    bucket = f"correctness-{suffix}"
    admin = psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        dbname="postgres",
        connect_timeout=5,
    )
    admin.autocommit = True
    database_created = bucket_created = False
    patch = pytest.MonkeyPatch()
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            )
        database_created = True
        patch.setattr(settings, "DB_NAME", database)
        patch.setattr(settings, "S3_BUCKET_NAME", bucket)
        migration_env = dict(os.environ, DB_NAME=database, RUN_HANDLER_ENV="test")
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[1],
            env=migration_env,
            check=True,
            timeout=30,
        )
        asyncio.run(prepare_bucket())
        bucket_created = True
        yield
    finally:
        try:
            if bucket_created:
                asyncio.run(remove_bucket())
        finally:
            patch.undo()
            try:
                if database_created:
                    with admin.cursor() as cursor:
                        cursor.execute(
                            sql.SQL("DROP DATABASE {}").format(sql.Identifier(database))
                        )
            finally:
                admin.close()


@pytest_asyncio.fixture
async def client(isolated_storage):
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as http:
            yield http


@pytest_asyncio.fixture
async def db(isolated_storage):
    connection = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        timeout=5,
    )
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def s3(isolated_storage):
    async with s3_client() as client:
        yield client
