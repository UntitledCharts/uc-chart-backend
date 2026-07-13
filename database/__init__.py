from . import accounts
from . import charts
from . import comments
from . import external
from . import leaderboards
from . import oauth
from . import staff_actions

from .query import SelectQuery, ExecutableQuery

from asyncpg import Connection
from typing import TypeVar, Optional

T = TypeVar("T")


class DBConnWrapper:
    def __init__(self, conn: Connection):
        self.conn = conn

    async def execute(self, query: ExecutableQuery):
        return await self.conn.execute(query.sql, *query.args)

    async def fetch(self, query: SelectQuery[T]) -> Optional[list[T]]:
        fetch_result = await self.conn.fetch(query.sql, *query.args)

        if not fetch_result:
            return []

        return [query.model.model_validate(dict(x)) for x in fetch_result]

    async def fetchrow(self, query: SelectQuery[T]) -> Optional[T]:
        fetch_result = await self.conn.fetchrow(query.sql, *query.args)
        if not fetch_result:
            return None

        return query.model.model_validate(dict(fetch_result))
