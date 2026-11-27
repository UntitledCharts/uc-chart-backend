import argparse
import subprocess
from datetime import datetime
from pathlib import Path

from scripts._common import (
    add_config_argument,
    add_pg_bin_argument,
    find_pg_tool,
    format_bytes,
    pg_connection_arguments,
    pg_environment,
    read_config,
)

DUMP_DIR = Path(__file__).resolve().parent / "dumps"


def default_output() -> Path:
    """Pick a timestamped file in scripts/dumps, creating the folder git-ignored.

    Returns:
        Path: Output file.
    """
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    ignore = DUMP_DIR / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return DUMP_DIR / f"dump_{timestamp}.sql"


def main() -> None:
    """Dump the database to a plain SQL file with pg_dump."""
    parser = argparse.ArgumentParser(
        description=(
            "Dump the database to a plain SQL file with pg_dump (no owners, no ACLs, without "
            "the old pg_cron schema). Default output: scripts/dumps/dump_YYYY-MM-DD_HHMMSS.sql"
        )
    )
    add_config_argument(parser)
    add_pg_bin_argument(parser)
    parser.add_argument("output_file", nargs="?", help="where to write the dump")
    args = parser.parse_args()

    psql = read_config(args.config).database
    pg_dump = find_pg_tool("pg_dump", args.pg_bin)
    if args.output_file:
        output_file = Path(args.output_file)
        output_file.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_file = default_output()

    cmd = [
        pg_dump,
        *pg_connection_arguments(psql),
        "--no-owner",
        "--no-acl",
        "--exclude-schema=cron",
        "-f",
        str(output_file),
    ]

    print(f"Dumping database '{psql.database}' to {output_file}...")
    result = subprocess.run(cmd, env=pg_environment(psql))

    if result.returncode == 0:
        print(f"Done! ({format_bytes(output_file.stat().st_size)})")
    else:
        raise SystemExit(f"pg_dump failed with exit code {result.returncode}")


if __name__ == "__main__":
    main()
