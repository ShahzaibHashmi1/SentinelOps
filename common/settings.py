"""Configuration from environment variables (pydantic-settings).

Environment variable names are the upper-case field names, e.g. DB_POOL_MAX_SIZE.
Service-specific values (SERVICE_NAME, DATABASE_URL, ...) are set in docker-compose.yml.
"""
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    service_name: str = "unknown"
    app_version: str = "dev"  # set from the Docker build arg APP_VERSION
    log_level: str = "INFO"

    # Datastores (None when a service does not use that datastore)
    database_url: str | None = None
    redis_url: str | None = None

    # Downstream services (None when not used by this service)
    orders_url: str | None = None
    payments_url: str | None = None
    inventory_url: str | None = None

    # Outbound HTTP: one timeout (seconds) applied to connect/read/write/pool-wait
    http_timeout_seconds: float = 2.0
    http_max_connections: int = 50

    # Postgres connection pool: explicit size and wait timeout (design decision D3)
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    db_pool_timeout_seconds: float = 2.0

    @model_validator(mode="after")
    def _check_pool_sizes(self) -> "Settings":
        if self.db_pool_min_size > self.db_pool_max_size:
            raise ValueError("DB_POOL_MIN_SIZE must be <= DB_POOL_MAX_SIZE")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
