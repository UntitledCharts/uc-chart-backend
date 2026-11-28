import argparse
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from scripts._common import add_config_argument, connect, read_config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    """A SQL migration file.

    Attributes:
        version: Database version the file migrates to.
        slug: Short name taken from the file name.
        path: Location of the file.
    """

    version: int
    slug: str
    path: Path

    def sql(self) -> str:
        """Read the migration's SQL.

        Returns:
            str: The file contents.
        """
        return self.path.read_text(encoding="utf-8")


def find_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """List the migration files, checking their names and that versions are consecutive.

    Args:
        directory: Folder holding ``NNNN_<slug>.sql`` files.

    Returns:
        list[Migration]: Migrations ordered by version.
    """
    migrations: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME.match(path.name)
        if match is None:
            raise SystemExit(
                f"unexpected migration file name {path.name}: expected NNNN_<slug>.sql, "
                "NNNN being the zero-padded version it migrates to"
            )
        version = int(match.group(1))
        if version in migrations:
            raise SystemExit(
                f"two migrations target version {version}: "
                f"{migrations[version].path.name} and {path.name}"
            )
        migrations[version] = Migration(version, match.group(2), path)
    ordered = [migrations[version] for version in sorted(migrations)]
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.version != previous.version + 1:
            raise SystemExit(
                f"migration versions are not consecutive: {previous.path.name} "
                f"is followed by {current.path.name}"
            )
    return ordered


async def read_version(connection: asyncpg.Connection) -> int | None:
    """Read the database's schema version.

    Args:
        connection: Database connection.

    Returns:
        int | None: The version, or None when database_info doesn't exist.
    """
    exists = await connection.fetchval("SELECT to_regclass('public.database_info') IS NOT NULL")
    if not exists:
        return None
    records = await connection.fetch("SELECT version FROM database_info")
    if len(records) != 1:
        raise SystemExit(f"database_info must hold exactly one row, it holds {len(records)}")
    return int(records[0]["version"])


async def apply_migration(connection: asyncpg.Connection, migration: Migration) -> None:
    """Apply one migration and bump database_info in the same transaction.

    Args:
        connection: Database connection.
        migration: Migration to apply.
    """
    async with connection.transaction():
        locked = await connection.fetchval("SELECT version FROM database_info FOR UPDATE")
        if locked != migration.version - 1:
            raise RuntimeError(
                f"database_info changed to version {locked} while migrating, "
                "is another migration running?"
            )
        await connection.execute(migration.sql())
        await connection.execute("UPDATE database_info SET version = $1", migration.version)


async def run(args: argparse.Namespace) -> None:
    """Show or apply the pending migrations.

    Args:
        args: Parsed command line.
    """
    config = read_config(args.config)
    migrations = find_migrations()
    if not migrations:
        raise SystemExit(f"no migrations found in {MIGRATIONS_DIR}")
    base_version = migrations[0].version - 1
    latest_version = migrations[-1].version
    database = config.database
    connection = await connect(database)
    try:
        current = await read_version(connection)
        if current is None:
            raise SystemExit(
                f"database {database.database} has no database_info table. Create a new database "
                "with `python -m scripts.database_setup`, or bring an old one to version "
                f"{base_version} with the old backend's scripts/database_migration first."
            )
        print(
            f"Database {database.database} on {database.host}:{database.port} "
            f"is at version {current}."
        )
        if current < base_version:
            raise SystemExit(
                f"version {current} is older than the oldest supported version {base_version}. "
                f"Upgrade it to version {base_version} with the old backend's "
                "scripts/database_migration first."
            )
        if current > latest_version:
            raise SystemExit(
                f"version {current} is newer than the newest migration here ({latest_version}); "
                "this checkout is older than the database."
            )
        pending = [migration for migration in migrations if migration.version > current]
        if not pending:
            print("No pending migrations, the database is up to date.")
            return
        print(f"{len(pending)} pending migration(s):")
        for migration in pending:
            print(f"\n=== version {migration.version}: {migration.path.name} ===")
            print(migration.sql().rstrip())
        print()
        if not args.apply:
            print("Dry run, nothing was changed. Re-run with --apply to apply them.")
            return
        for migration in pending:
            print(f"Running migration to version {migration.version} ({migration.path.name})...")
            try:
                await apply_migration(connection, migration)
            except (
                asyncpg.PostgresError,
                asyncpg.InterfaceError,
                RuntimeError,
            ) as error:
                raise SystemExit(
                    f"Migration {migration.path.name} failed and was rolled back, the database "
                    f"stays at version {migration.version - 1}: {error}"
                ) from None
            print(f"Migration to version {migration.version} complete.")
        print("All migrations complete.")
    finally:
        await connection.close()


def main() -> None:
    """Parse the command line and run the migration runner."""
    parser = argparse.ArgumentParser(
        description=(
            "Apply scripts/migrations/NNNN_<slug>.sql files newer than database_info.version. "
            "Dry run by default: prints the current version and the SQL of each pending migration."
        )
    )
    add_config_argument(parser)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the pending migrations, each in its own transaction, stopping at the first failure",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
