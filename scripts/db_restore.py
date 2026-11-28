import argparse
import subprocess
import tempfile
from pathlib import Path

from scripts._common import (
    add_config_argument,
    add_pg_bin_argument,
    confirm,
    find_pg_tool,
    pg_connection_arguments,
    pg_environment,
    read_config,
)

CUSTOM_FORMAT_MAGIC = b"PGDMP"
RESET_SCHEMA = "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"


def is_custom_format(path: Path) -> bool:
    """Check whether a dump is in pg_dump's custom format instead of plain SQL.

    Args:
        path: Dump file.

    Returns:
        bool: True for custom-format archives.
    """
    with path.open("rb") as handle:
        return handle.read(len(CUSTOM_FORMAT_MAGIC)) == CUSTOM_FORMAT_MAGIC


def restore_sql(psql_arguments: list[str], sql_file: Path, env: dict[str, str]) -> None:
    """Run the schema reset and a SQL file in one psql transaction, exiting when it fails.

    Args:
        psql_arguments: psql command resetting the schema in a single transaction.
        sql_file: SQL run after the reset.
        env: Environment for psql.
    """
    result = subprocess.run([*psql_arguments, "-f", str(sql_file)], env=env)
    if result.returncode != 0:
        raise SystemExit(
            f"Restore failed with exit code {result.returncode} and was rolled back together "
            "with the schema drop; the database is unchanged."
        )


def main() -> None:
    """Replace the database's public schema with a dump."""
    parser = argparse.ArgumentParser(
        description=(
            "Restore a dump into the configured database. WARNING: drops and recreates the "
            "public schema first, so every existing table and row is lost. Plain SQL dumps are "
            "run with psql; custom-format archives are converted to SQL with pg_restore first. "
            "The drop and the restore run in one transaction, so a failed restore changes nothing."
        )
    )
    add_config_argument(parser)
    add_pg_bin_argument(parser)
    parser.add_argument("input_file", help="dump to restore")
    args = parser.parse_args()

    input_file = Path(args.input_file)
    if not input_file.is_file():
        raise SystemExit(f"File not found: {input_file}")

    psql = read_config(args.config).database
    custom = is_custom_format(input_file)
    psql_tool = find_pg_tool("psql", args.pg_bin)
    restore_tool = find_pg_tool("pg_restore", args.pg_bin) if custom else psql_tool

    print(
        f"This will restore '{input_file}' into database '{psql.database}' "
        f"on {psql.host}:{psql.port}."
    )
    print("WARNING: Existing data will be overwritten.")
    if not confirm("Type 'yes' to continue:", "yes"):
        print("Aborted.")
        return

    env = pg_environment(psql)
    psql_arguments = [
        psql_tool,
        *pg_connection_arguments(psql),
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        "-c",
        RESET_SCHEMA,
    ]

    if custom:
        with tempfile.TemporaryDirectory(prefix="db_restore_") as temp_dir:
            sql_file = Path(temp_dir) / "restore.sql"
            print(f"Converting {input_file} to SQL...")
            result = subprocess.run(
                [restore_tool, "--no-owner", "--no-acl", "-f", str(sql_file), str(input_file)]
            )
            if result.returncode != 0:
                raise SystemExit(
                    f"Reading the archive failed with exit code {result.returncode}; "
                    "nothing was changed."
                )
            print(f"Restoring from {input_file}...")
            restore_sql(psql_arguments, sql_file, env)
    else:
        print(f"Restoring from {input_file}...")
        restore_sql(psql_arguments, input_file, env)
    print("Done! Run `python -m scripts.apply_database` to check for pending migrations.")


if __name__ == "__main__":
    main()
