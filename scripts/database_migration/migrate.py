import asyncio
from typing import Awaitable, Callable

import asyncpg
import yaml

from . import migrate_1_to_2, migrate_2_to_3, migrate_3_to_4

with open("config.yml", "r") as f:
    config = yaml.load(f, yaml.Loader)

psql_config = config["psql"]

migrations: dict[int, Callable[[asyncpg.Connection], Awaitable[int]]] = {
    1: migrate_1_to_2.run,
    2: migrate_2_to_3.run,
    3: migrate_3_to_4.run,
}


async def _ensure_version(conn: asyncpg.Connection) -> int:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS database_info (version INTEGER PRIMARY KEY);"
    )
    # Databases created before versioning existed start at the baseline.
    await conn.execute(
        "INSERT INTO database_info (version) SELECT 1 "
        "WHERE NOT EXISTS (SELECT 1 FROM database_info);"
    )
    version = await conn.fetchval("SELECT version FROM database_info LIMIT 1;")
    assert version is not None
    return int(version)


async def main() -> None:
    conn = await asyncpg.connect(
        host=psql_config["host"],
        user=psql_config["user"],
        database=psql_config["database"],
        password=psql_config["password"],
        port=psql_config["port"],
        ssl="disable",
    )
    try:
        current_version = await _ensure_version(conn)
        while current_version in migrations:
            print(f"Running migration for version {current_version}...")
            async with conn.transaction():
                new_version = await migrations[current_version](conn)
                await conn.execute(
                    "UPDATE database_info SET version = $1;", new_version
                )
            current_version = new_version
            print(f"Migration to version {current_version} complete.")
        print("All migrations complete.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
