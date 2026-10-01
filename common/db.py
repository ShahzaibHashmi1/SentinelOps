"""Postgres access ONLY through one psycopg AsyncConnectionPool with explicit size and timeout."""
import math
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from common.errors import describe_exception, error_body, remember_error
from common.metrics import DB_POOL_IN_USE, DB_POOL_MAX, track_dependency
from common.settings import Settings


class Database:
    def __init__(self, settings: Settings):
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is required for this service")
        self.settings = settings
        self._in_use = 0
        self.pool = AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            timeout=settings.db_pool_timeout_seconds,  # max wait for a free connection
            name=settings.service_name,
            open=False,
            # check: test a connection before handing it out, so connections broken by a
            # postgres restart are replaced instead of causing errors (auto-recovery).
            check=AsyncConnectionPool.check_connection,
            # Reconnect backoff doubles (1, 2, 4, ... s) until reconnect_timeout. The default of 300 s
            # would let it grow to minutes during a long outage; 10 s keeps recovery fast: after
            # giving up, the next request starts a fresh attempt cycle with a 1 s delay.
            reconnect_timeout=10.0,
            kwargs={
                "autocommit": True,  # every statement is its own transaction
                "row_factory": dict_row,
                "connect_timeout": max(1, math.ceil(settings.db_pool_timeout_seconds)),
            },
        )

    async def open(self) -> None:
        # wait=False: the service starts even if postgres is down; the pool keeps
        # retrying in the background, which is what lets it recover by itself.
        await self.pool.open(wait=False)
        DB_POOL_MAX.set(self.settings.db_pool_max_size)
        DB_POOL_IN_USE.set_function(lambda: self._in_use)

    async def close(self) -> None:
        await self.pool.close()

    @asynccontextmanager
    async def _connection(self, timeout: float | None = None):
        async with self.pool.connection(timeout=timeout) as conn:
            self._in_use += 1
            try:
                yield conn
            finally:
                self._in_use -= 1

    async def fetch_all(self, sql: str, params=None) -> list[dict]:
        with track_dependency("postgres"):
            async with self._connection() as conn:
                cur = await conn.execute(sql, params)
                return await cur.fetchall()

    async def fetch_one(self, sql: str, params=None) -> dict | None:
        with track_dependency("postgres"):
            async with self._connection() as conn:
                cur = await conn.execute(sql, params)
                return await cur.fetchone()

    async def execute(self, sql: str, params=None) -> int:
        """Run a statement and return the number of affected rows."""
        with track_dependency("postgres"):
            async with self._connection() as conn:
                cur = await conn.execute(sql, params)
                return cur.rowcount

    async def ping(self) -> None:
        async with self._connection(timeout=1.0) as conn:
            await conn.execute("SELECT 1")


async def check_postgres(app: FastAPI) -> None:
    """Readiness check used by /ready."""
    await app.state.db.ping()


async def _db_unavailable(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
    # PoolTimeout (no free connection in time), connection refused, server closed the connection...
    remember_error(request, f"database_unavailable: {describe_exception(exc)}")
    return JSONResponse(error_body("database_unavailable"), status_code=503)


def register_db_error_handler(app: FastAPI) -> None:
    """Turn any postgres connectivity error into a fast 503 JSON response."""
    app.add_exception_handler(psycopg.OperationalError, _db_unavailable)
