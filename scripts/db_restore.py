import argparse
import subprocess
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


def main() -> None:
    """Replace the database's public schema with a dump."""
    parser = argparse.ArgumentParser(
        description=(
            "Restore a dump into the configured database. WARNING: drops and recreates the "
            "public schema first, so every existing table and row is lost. Plain SQL dumps are "
            "run with psql, custom-format archives with pg_restore."
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
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        "-c",
        RESET_SCHEMA,
    ]

    if custom:
        print("Dropping existing schema...")
        result = subprocess.run(psql_arguments, env=env)
        if result.returncode != 0:
            raise SystemExit(
                f"Schema drop failed with exit code {result.returncode}; nothing was changed."
            )
        restore_cmd = [
            restore_tool,
            *pg_connection_arguments(psql),
            "--no-owner",
            "--no-acl",
            "--single-transaction",
            "--exit-on-error",
            str(input_file),
        ]
        failure = (
            "Restore failed with exit code {code} and was rolled back. The public schema was "
            "already dropped, so the database is now EMPTY: fix the error and restore again."
        )
    else:
        restore_cmd = [*psql_arguments, "-f", str(input_file)]
        failure = (
            "Restore failed with exit code {code} and was rolled back together with the schema "
            "drop; the database is unchanged."
        )

    print(f"Restoring from {input_file}...")
    result = subprocess.run(restore_cmd, env=env)

    if result.returncode != 0:
        raise SystemExit(failure.format(code=result.returncode))
    print("Done! Run `python -m scripts.apply_database` to check for pending migrations.")


if __name__ == "__main__":
    main()
