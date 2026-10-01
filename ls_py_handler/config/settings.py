"""
Settings module for the LS Run Handler.

This module provides configuration settings for the application,
with values loaded from environment variables and sensible defaults.
"""
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    # App settings
    APP_TITLE: str = "LS Run Handler"
    APP_DESCRIPTION: str = "A simple FastAPI server with run endpoints"
    APP_VERSION: str = "0.1.0"

    # Database settings
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "postgres"
    DB_PASSWORD: str = "postgres"
    DB_NAME: str = "postgres"
    DB_POOL_MIN_SIZE: int = 2
    DB_POOL_MAX_SIZE: int = 20
    DB_POOL_ACQUIRE_TIMEOUT_SECONDS: float = 5.0
    DB_COMMAND_TIMEOUT_SECONDS: float = 30.0

    # S3/MinIO settings
    S3_BUCKET_NAME: str = "runs"
    S3_ENDPOINT_URL: str = "http://localhost:9002"
    S3_ACCESS_KEY: str = "minioadmin1"
    S3_SECRET_KEY: str = "minioadmin1"
    S3_REGION: str = "us-east-1"
    S3_CONNECT_TIMEOUT_SECONDS: float = 3.0
    S3_READ_TIMEOUT_SECONDS: float = 30.0
    S3_MAX_CONNECTIONS: int = 50
    S3_MAX_ATTEMPTS: int = 3

    # Request limits. Content length is checked before parsing when available,
    # and the encoded size is checked again before any external writes.
    MAX_REQUEST_BYTES: int = 64 * 1024 * 1024
    MAX_BATCH_BYTES: int = 64 * 1024 * 1024
    MAX_BATCH_RUNS: int = 1_000

    # Configure pydantic to read from .env file
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )


# Create a global settings instance
settings = (
    Settings(_env_file=".env.test")
    if os.getenv("RUN_HANDLER_ENV") == "test"
    else Settings()
)
