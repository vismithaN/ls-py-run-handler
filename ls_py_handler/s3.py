from functools import lru_cache

from aiobotocore.session import get_session


@lru_cache(maxsize=1)
def get_s3_session():
    """Reuse SDK session setup, while keeping S3 clients request-scoped."""
    return get_session()
