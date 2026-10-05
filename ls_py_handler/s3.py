"""Share cached S3 SDK session setup between startup and request handling.

The session is shared within a process; clients and their connections are still
created and closed by each caller's async context manager.
"""

from functools import lru_cache

from aiobotocore.session import get_session


@lru_cache(maxsize=1)
def get_s3_session():
    """Return the process-cached aiobotocore session, creating it on first use.

    Reusing SDK setup avoids constructing a session for every request. This cache
    does not retain an S3 client or pool network connections across requests.
    Callers supply endpoint, credentials, and region when creating a client, and
    close that client with an async context manager.
    """
    return get_session()
