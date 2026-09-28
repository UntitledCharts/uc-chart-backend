import asyncpg


async def run(conn: asyncpg.Connection) -> int:
    await conn.execute(
        """
        ALTER TABLE promotions ADD COLUMN IF NOT EXISTS logged_in_click_count BIGINT NOT NULL DEFAULT 0;
        """
    )

    return 5
