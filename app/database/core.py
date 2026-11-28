from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import asyncpg
from asyncpg.pool import PoolConnectionProxy
from pydantic import BaseModel

from app.settings import DatabaseConfig

ModelT = TypeVar("ModelT", bound=BaseModel)

Connection = asyncpg.Connection | PoolConnectionProxy
SCHEMA_VERSION = 6


class Database:
    """asyncpg pool wrapper.

    Attributes:
        pool: The connection pool, created by ``open``.
    """

    def __init__(self) -> None:
        """Create an unopened database wrapper."""
        self.pool: asyncpg.Pool | None = None

    async def open(self, config: DatabaseConfig) -> None:
        """Open the connection pool.

        Args:
            config: Database settings.
        """
        self.pool = await asyncpg.create_pool(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            min_size=config.pool_min_size,
            max_size=config.pool_max_size,
            ssl=config.ssl,
        )

    async def close(self) -> None:
        """Close the connection pool."""
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[Connection, None]:
        """Borrow a connection from the pool.

        Returns:
            AsyncGenerator[Connection, None]: Context yielding the connection.
        """
        if self.pool is None:
            raise RuntimeError("database pool is not open")
        async with self.pool.acquire() as connection:
            yield connection

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[Connection, None]:
        """Borrow a connection and run a transaction on it.

        Returns:
            AsyncGenerator[Connection, None]: Context yielding the connection inside the transaction.
        """
        async with self.connection() as connection:
            async with connection.transaction():
                yield connection


async def check_schema_version(database: "Database") -> None:
    """Refuse to run against a database that isn't at the schema version this code was written for.

    Args:
        database: Open database pool.
    """
    async with database.connection() as connection:
        exists = await connection.fetchval("SELECT to_regclass('public.database_info') IS NOT NULL")
        versions = await connection.fetch("SELECT version FROM database_info") if exists else []
    found = versions[0]["version"] if len(versions) == 1 else None
    if found != SCHEMA_VERSION:
        raise RuntimeError(
            f"the database is at schema version {found}, this code needs {SCHEMA_VERSION}; "
            "run `python -m scripts.apply_database --apply` first"
        )


async def fetch_one(
    connection: Connection, model: type[ModelT], query: str, *args: Any
) -> ModelT | None:
    """Fetch one row as a model.

    Args:
        connection: Connection to query on.
        model: Model to validate the row into.
        query: SQL query.
        *args: Query parameters.

    Returns:
        ModelT | None: The row, or None when there is none.
    """
    record = await connection.fetchrow(query, *args)
    return None if record is None else model.model_validate(dict(record))


async def fetch_all(
    connection: Connection, model: type[ModelT], query: str, *args: Any
) -> list[ModelT]:
    """Fetch every row as models.

    Args:
        connection: Connection to query on.
        model: Model to validate each row into.
        query: SQL query.
        *args: Query parameters.

    Returns:
        list[ModelT]: The rows.
    """
    records = await connection.fetch(query, *args)
    return [model.model_validate(dict(record)) for record in records]


async def fetch_value(connection: Connection, query: str, *args: Any) -> Any:
    """Fetch the first column of the first row.

    Args:
        connection: Connection to query on.
        query: SQL query.
        *args: Query parameters.

    Returns:
        Any: The value, or None when there is no row.
    """
    return await connection.fetchval(query, *args)


def placeholders(start: int, count: int) -> str:
    """Build a comma-separated list of positional placeholders.

    Args:
        start: Number of the first placeholder.
        count: How many placeholders to build.

    Returns:
        str: Text like ``$3, $4, $5``.
    """
    return ", ".join(f"${index}" for index in range(start, start + count))


class QueryBuilder:
    """Collects WHERE conditions and their parameters.

    Attributes:
        conditions: SQL conditions joined with AND.
        params: Positional parameters in order.
    """

    def __init__(self, params: Sequence[Any] = ()) -> None:
        """Create a builder.

        Args:
            params: Parameters already used by the query.
        """
        self.conditions: list[str] = []
        self.params: list[Any] = list(params)

    def param(self, value: Any) -> str:
        """Add a parameter.

        Args:
            value: Parameter value.

        Returns:
            str: Its placeholder.
        """
        self.params.append(value)
        return f"${len(self.params)}"

    def where(self, condition: str) -> None:
        """Add a condition.

        Args:
            condition: SQL condition.
        """
        self.conditions.append(condition)

    def where_clause(self) -> str:
        """Render the collected conditions.

        Returns:
            str: A WHERE clause, or an empty string when there are no conditions.
        """
        if not self.conditions:
            return ""
        return "WHERE " + " AND ".join(self.conditions)
