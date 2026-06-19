"""The only direct database connection manager in Loom.

Async access (FastAPI, MCP-async paths) uses asyncpg. Sync access (MCP stdio
server) uses psycopg2. Vectors are passed as ``$n::vector`` casts with string
literals, so no pgvector codec registration is required.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog

from loom import constants
from loom.errors import LoomDBError

if TYPE_CHECKING:
    from loom.config import LoomConfig

logger = structlog.get_logger("loom.db")

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "supabase" / "schema.sql"
_MIGRATION_VERSION = "0001_initial"


def _load_schema_sql() -> str:
    if not SCHEMA_PATH.exists():
        raise LoomDBError(
            "Schema file not found.",
            details={"path": str(SCHEMA_PATH)},
        )
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _sanitized_sql_prefix(query: str, *, length: int = 60) -> str:
    flattened = " ".join(query.split())
    return flattened[:length]


async def probe_database_async(database_url: str) -> float:
    """Connect, run ``SELECT 1``, return latency in milliseconds.

    Raises on failure. Used for credential checks.
    """
    import asyncpg

    start = time.perf_counter()
    conn = await asyncpg.connect(dsn=database_url)
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()
    return (time.perf_counter() - start) * 1000.0


def probe_database_sync(database_url: str) -> float:
    """Sync variant of :func:`probe_database_async`."""
    import psycopg2

    start = time.perf_counter()
    conn = psycopg2.connect(dsn=database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    finally:
        conn.close()
    return (time.perf_counter() - start) * 1000.0


class DatabasePool:
    """The single database access point for Loom."""

    def __init__(self, config: LoomConfig, mode: Literal["async", "sync"] = "async"):
        self.config = config
        self.mode = mode
        self._async_pool: Any = None
        self._sync_pool: Any = None

    # ------------------------------------------------------------------
    # Async lifecycle
    # ------------------------------------------------------------------
    async def init_async(self) -> None:
        if self.mode != "async":
            raise LoomDBError("init_async called on a non-async pool.")
        if self._async_pool is not None:
            return
        import asyncpg

        try:
            self._async_pool = await asyncpg.create_pool(
                dsn=self.config.database_url,
                min_size=constants.DB_MIN_SIZE,
                max_size=constants.DB_MAX_SIZE,
                command_timeout=constants.DB_COMMAND_TIMEOUT_SECONDS,
                max_inactive_connection_lifetime=(
                    constants.DB_MAX_INACTIVE_CONNECTION_LIFETIME_SECONDS
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise LoomDBError(
                "Failed to create database pool.",
                details={"error_type": type(exc).__name__},
            ) from exc

        await self.run_migrations_async()
        await self._verify_pgvector_async()

    async def close_async(self) -> None:
        if self._async_pool is not None:
            await self._async_pool.close()
            self._async_pool = None

    @asynccontextmanager
    async def acquire(self):
        if self.mode != "async" or self._async_pool is None:
            raise LoomDBError("Async pool not initialized.")
        async with self._async_pool.acquire() as conn:
            yield conn

    async def _timed_async(self, op: str, query: str, coro):
        start = time.perf_counter()
        try:
            result = await coro
        except Exception as exc:  # noqa: BLE001
            raise LoomDBError(
                "Database query failed.",
                details={
                    "op": op,
                    "sql_prefix": _sanitized_sql_prefix(query),
                    "error_type": type(exc).__name__,
                },
            ) from exc
        duration_ms = (time.perf_counter() - start) * 1000.0
        if duration_ms > constants.DB_SLOW_QUERY_MS:
            logger.warning(
                "slow_query",
                op=op,
                sql_prefix=_sanitized_sql_prefix(query),
                duration_ms=round(duration_ms, 1),
                mode="async",
            )
        return result

    async def fetch(self, query: str, *args) -> list[Any]:
        async with self.acquire() as conn:
            return await self._timed_async("fetch", query, conn.fetch(query, *args))

    async def fetchrow(self, query: str, *args) -> Any | None:
        async with self.acquire() as conn:
            return await self._timed_async(
                "fetchrow", query, conn.fetchrow(query, *args)
            )

    async def fetchval(self, query: str, *args) -> Any:
        async with self.acquire() as conn:
            return await self._timed_async(
                "fetchval", query, conn.fetchval(query, *args)
            )

    async def execute(self, query: str, *args) -> str:
        async with self.acquire() as conn:
            return await self._timed_async("execute", query, conn.execute(query, *args))

    async def run_migrations_async(self) -> None:
        schema_sql = _load_schema_sql()
        async with self.acquire() as conn:
            try:
                await conn.execute(schema_sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1) "
                    "ON CONFLICT (version) DO NOTHING",
                    _MIGRATION_VERSION,
                )
            except Exception as exc:  # noqa: BLE001
                raise LoomDBError(
                    "Failed to run migrations.",
                    details={"error_type": type(exc).__name__},
                ) from exc

    async def _verify_pgvector_async(self) -> None:
        row = await self.fetchrow(
            "SELECT 1 AS ok FROM pg_extension WHERE extname = 'vector'"
        )
        if row is None:
            raise LoomDBError("pgvector extension is not enabled.")

    async def health_check_async(self) -> dict:
        start = time.perf_counter()
        try:
            await self.fetchval("SELECT 1")
        except LoomDBError:
            return {"connected": False, "latency_ms": None}
        latency_ms = (time.perf_counter() - start) * 1000.0
        return {"connected": True, "latency_ms": round(latency_ms, 2)}

    # ------------------------------------------------------------------
    # Sync lifecycle
    # ------------------------------------------------------------------
    def init_sync(self) -> None:
        if self.mode != "sync":
            raise LoomDBError("init_sync called on a non-sync pool.")
        if self._sync_pool is not None:
            return
        import psycopg2.pool

        try:
            self._sync_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=constants.DB_SYNC_MIN_SIZE,
                maxconn=constants.DB_SYNC_MAX_SIZE,
                dsn=self.config.database_url,
            )
        except Exception as exc:  # noqa: BLE001
            raise LoomDBError(
                "Failed to create sync database pool.",
                details={"error_type": type(exc).__name__},
            ) from exc

        self.run_migrations_sync()
        self._verify_pgvector_sync()

    def close_sync(self) -> None:
        if self._sync_pool is not None:
            self._sync_pool.closeall()
            self._sync_pool = None

    @contextmanager
    def acquire_sync(self):
        if self.mode != "sync" or self._sync_pool is None:
            raise LoomDBError("Sync pool not initialized.")
        conn = self._sync_pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._sync_pool.putconn(conn)

    def _timed_sync(self, op: str, query: str, fn):
        start = time.perf_counter()
        try:
            result = fn()
        except LoomDBError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LoomDBError(
                "Database query failed.",
                details={
                    "op": op,
                    "sql_prefix": _sanitized_sql_prefix(query),
                    "error_type": type(exc).__name__,
                },
            ) from exc
        duration_ms = (time.perf_counter() - start) * 1000.0
        if duration_ms > constants.DB_SLOW_QUERY_MS:
            logger.warning(
                "slow_query",
                op=op,
                sql_prefix=_sanitized_sql_prefix(query),
                duration_ms=round(duration_ms, 1),
                mode="sync",
            )
        return result

    def fetch_sync(self, query: str, params: tuple = ()) -> list[dict]:
        def _run() -> list[dict]:
            with self.acquire_sync() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    columns = [d[0] for d in cur.description] if cur.description else []
                    rows = cur.fetchall()
                    return [dict(zip(columns, row, strict=False)) for row in rows]

        return self._timed_sync("fetch_sync", query, _run)

    def fetchrow_sync(self, query: str, params: tuple = ()) -> dict | None:
        def _run() -> dict | None:
            with self.acquire_sync() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    if cur.description is None:
                        return None
                    columns = [d[0] for d in cur.description]
                    row = cur.fetchone()
                    return dict(zip(columns, row, strict=False)) if row else None

        return self._timed_sync("fetchrow_sync", query, _run)

    def execute_sync(self, query: str, params: tuple = ()) -> None:
        def _run() -> None:
            with self.acquire_sync() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)

        self._timed_sync("execute_sync", query, _run)

    def run_migrations_sync(self) -> None:
        schema_sql = _load_schema_sql()
        try:
            with self.acquire_sync() as conn:
                with conn.cursor() as cur:
                    cur.execute(schema_sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s) "
                        "ON CONFLICT (version) DO NOTHING",
                        (_MIGRATION_VERSION,),
                    )
        except LoomDBError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LoomDBError(
                "Failed to run sync migrations.",
                details={"error_type": type(exc).__name__},
            ) from exc

    def _verify_pgvector_sync(self) -> None:
        row = self.fetchrow_sync(
            "SELECT 1 AS ok FROM pg_extension WHERE extname = 'vector'"
        )
        if row is None:
            raise LoomDBError("pgvector extension is not enabled.")

    def health_check_sync(self) -> dict:
        start = time.perf_counter()
        try:
            self.fetchrow_sync("SELECT 1 AS ok")
        except LoomDBError:
            return {"connected": False, "latency_ms": None}
        latency_ms = (time.perf_counter() - start) * 1000.0
        return {"connected": True, "latency_ms": round(latency_ms, 2)}
