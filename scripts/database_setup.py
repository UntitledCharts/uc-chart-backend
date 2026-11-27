import argparse
import asyncio
from pathlib import Path

import asyncpg

from scripts._common import add_config_argument, connect, read_config
from scripts.apply_database import find_migrations

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


async def run(args: argparse.Namespace) -> None:
    """Create the schema in an empty database.

    Args:
        args: Parsed command line.
    """
    config = read_config(args.config)
    migrations = find_migrations()
    if not migrations:
        raise SystemExit("no migration files found, can't tell which version schema.sql is")
    version = migrations[-1].version
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    database = config.database
    connection = await connect(database)
    try:
        records = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        if records:
            names = ", ".join(record["tablename"] for record in records[:10])
            more = ", ..." if len(records) > 10 else ""
            raise SystemExit(
                f"refusing to set up {database.database}: it already has {len(records)} public "
                f"table(s) ({names}{more}). database_setup only creates fresh databases; "
                "migrate existing ones with `python -m scripts.apply_database`."
            )
        print(f"Creating schema version {version} in {database.database}...")
        try:
            async with connection.transaction():
                await connection.execute(schema)
                await connection.execute("INSERT INTO database_info (version) VALUES ($1)", version)
        except asyncpg.PostgresError as error:
            raise SystemExit(
                f"setup failed and was rolled back, nothing was created: {error}"
            ) from None
        count = await connection.fetchval(
            "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public'"
        )
        print(f"Done! Created {count} tables at version {version}.")
    finally:
        await connection.close()


def main() -> None:
    """Parse the command line and set up the database."""
    parser = argparse.ArgumentParser(
        description=(
            "Create a fresh database from scripts/schema.sql and mark it with the latest "
            "migration version. Refuses to run on a database that already has tables. "
            "pg_cron is not needed: the API's background worker publishes scheduled charts "
            "and deletes expired rows."
        )
    )
    add_config_argument(parser)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
