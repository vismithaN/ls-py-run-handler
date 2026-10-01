import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ls_py_handler.api.routes.runs import router as runs_router
from ls_py_handler.config.settings import settings
from ls_py_handler.observability import log_event
from ls_py_handler.resources import lifespan

logging.basicConfig(level=logging.INFO, format="%(message)s")

app = FastAPI(
    title=settings.APP_TITLE,
    description=settings.APP_DESCRIPTION,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# Include routers
app.include_router(runs_router)


@app.middleware("http")
async def request_context(request, call_next):
    """Apply an early body limit and emit payload-safe request timing logs."""

    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id

    if request.method == "POST" and request.url.path == "/runs":
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.MAX_REQUEST_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body exceeds the configured limit"},
                        headers={"X-Request-ID": request_id},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                    headers={"X-Request-ID": request_id},
                )

    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log_event(
            "http_request_failed",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            duration_ms=round((time.perf_counter() - started_at) * 1_000, 3),
        )
        raise

    response.headers["X-Request-ID"] = request_id
    log_event(
        "http_request_completed",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=round((time.perf_counter() - started_at) * 1_000, 3),
    )
    return response


@app.get("/")
async def root():
    """
    Root endpoint to verify the API is running.
    """
    return {"message": settings.APP_TITLE + " API"}


@app.get("/health/live", include_in_schema=False)
async def liveness():
    """Confirm that the process and event loop can serve requests."""

    return {"status": "alive"}


@app.get("/health/ready", include_in_schema=False)
async def readiness(request: Request):
    """Confirm that both required storage dependencies are reachable."""

    resources = request.app.state.resources
    try:
        async with resources.db_pool.acquire(timeout=1.0) as connection:
            await connection.fetchval("SELECT 1")
        await resources.s3.head_bucket(Bucket=settings.S3_BUCKET_NAME)
    except Exception as exc:
        log_event(
            "readiness_check_failed",
            request_id=request.state.request_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail="Dependencies are not ready"
        ) from exc
    return {"status": "ready"}
