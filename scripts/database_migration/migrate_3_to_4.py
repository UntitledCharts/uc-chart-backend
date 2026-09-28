import asyncpg


async def run(conn: asyncpg.Connection) -> int:
    await conn.execute(
        """
        ALTER TABLE promotions ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ DEFAULT NULL;
        """
    )

    return 4
