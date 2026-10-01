"""Reset only the dedicated test database and object-storage bucket."""

import asyncio

import psycopg2
from aiobotocore.session import get_session
from botocore.exceptions import ClientError
from psycopg2 import sql

from ls_py_handler.config.settings import settings


def reset_database() -> None:
    """Drop and recreate the configured test database safely."""

    connection = psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database="postgres",
    )
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (settings.DB_NAME,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(settings.DB_NAME)
                )
            )
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.DB_NAME))
            )
    finally:
        connection.close()


async def reset_bucket() -> None:
    """Delete all objects from and recreate the configured test bucket."""

    session = get_session()
    async with session.create_client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
    ) as s3:
        try:
            continuation_token = None
            while True:
                request = {"Bucket": settings.S3_BUCKET_NAME}
                if continuation_token:
                    request["ContinuationToken"] = continuation_token
                response = await s3.list_objects_v2(**request)
                objects = [
                    {"Key": item["Key"]} for item in response.get("Contents", [])
                ]
                if objects:
                    await s3.delete_objects(
                        Bucket=settings.S3_BUCKET_NAME,
                        Delete={"Objects": objects, "Quiet": True},
                    )
                if not response.get("IsTruncated"):
                    break
                continuation_token = response["NextContinuationToken"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchBucket":
                raise
            await s3.create_bucket(Bucket=settings.S3_BUCKET_NAME)


def main() -> None:
    if settings.DB_NAME != "postgres_test" or settings.S3_BUCKET_NAME != "runs-test":
        raise RuntimeError(
            "Refusing to reset resources that are not the dedicated test resources"
        )
    reset_database()
    asyncio.run(reset_bucket())


if __name__ == "__main__":
    main()
