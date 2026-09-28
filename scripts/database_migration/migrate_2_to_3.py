import asyncpg


async def run(conn: asyncpg.Connection) -> int:
    await conn.execute(
        """
        ALTER TABLE accounts ADD COLUMN IF NOT EXISTS ad_views BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE accounts ADD COLUMN IF NOT EXISTS ad_clicks BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE accounts ADD COLUMN IF NOT EXISTS recent_opened_levels INTEGER[] NOT NULL DEFAULT '{}';
        """
    )

    return 3
