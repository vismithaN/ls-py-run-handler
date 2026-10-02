"""Reset runs-test through the S3 API, without requiring mc in the container."""

import asyncio
import os
from urllib.parse import urlparse

from aiobotocore.session import get_session
from botocore.config import Config
from botocore.exceptions import ClientError

from ls_py_handler.config.settings import settings


async def reset_test_bucket():
    if (
        os.environ.get("RUN_HANDLER_ENV") != "test"
        or settings.S3_BUCKET_NAME != "runs-test"
        or urlparse(settings.S3_ENDPOINT_URL).hostname
        not in {"localhost", "127.0.0.1", "::1"}
    ):
        raise RuntimeError("Reset requires RUN_HANDLER_ENV=test and local runs-test")

    async with get_session().create_client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 0}),
    ) as s3:
        try:
            await s3.head_bucket(Bucket="runs-test")
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
        else:
            async for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket="runs-test"
            ):
                for item in page.get("Contents", []):
                    await s3.delete_object(Bucket="runs-test", Key=item["Key"])
            await s3.delete_bucket(Bucket="runs-test")

        await s3.create_bucket(Bucket="runs-test")
        print("Recreated runs-test; previous test objects removed")


if __name__ == "__main__":
    asyncio.run(reset_test_bucket())
