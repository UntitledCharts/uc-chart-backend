import asyncpg


async def run(conn: asyncpg.Connection) -> int:
    await conn.execute(
        """
        ALTER TABLE accounts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ DEFAULT NULL;
        ALTER TABLE charts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ DEFAULT NULL;
        CREATE INDEX IF NOT EXISTS idx_accounts_deleted_at ON accounts(deleted_at) WHERE deleted_at IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_charts_deleted_at ON charts(deleted_at) WHERE deleted_at IS NOT NULL;
        """
    )

    return 2
