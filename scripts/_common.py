import argparse
import asyncio
import io
import os
import shutil
import sys
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import asyncpg
from pydantic import ValidationError

from app.auth.principal import Principal
from app.auth.sonolus import load_sonolus_key
from app.context import AppContext
from app.database import accounts as accounts_db
from app.database.accounts import AccountRow
from app.database.core import Connection, Database
from app.services import staff_log
from app.services.conversion_runner import ConversionChildren
from app.services.crypto import TokenCipher
from app.services.discord_bot import DiscordBot
from app.services.storage import Storage
from app.settings import Config, Constants, DatabaseConfig, load_config, load_constants

ADMIN_KEY = Principal(kind="admin_key")
SCRIPT_POOL_SIZE = 4


def _tolerate_unencodable_output() -> None:
    """Replace characters the terminal can't encode instead of crashing on names and titles."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


_tolerate_unencodable_output()


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    """Add the --config option to a parser.

    Args:
        parser: Parser to extend.
    """
    parser.add_argument("--config", default="config.yml", help="path to config.yml")


def add_constants_argument(parser: argparse.ArgumentParser) -> None:
    """Add the --constants option to a parser.

    Args:
        parser: Parser to extend.
    """
    parser.add_argument("--constants", default="constants.yml", help="path to constants.yml")


def add_account_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the required, mutually exclusive --id and --handle options to a parser.

    Args:
        parser: Parser to extend.
    """
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", dest="account_id", help="Sonolus id of the account")
    target.add_argument("--handle", type=int, help="Sonolus handle of the account")


def _validation_summary(error: ValidationError) -> str:
    """Describe a validation error without echoing the offending values.

    Args:
        error: Pydantic validation error.

    Returns:
        str: One line per problem with its location and message.
    """
    lines = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "(root)"
        lines.append(f"  {location}: {item['msg']}")
    return "\n".join(lines)


def read_config(path: str) -> Config:
    """Load config.yml, exiting with a value-free message when it is invalid.

    Args:
        path: Location of config.yml.

    Returns:
        Config: Validated configuration.
    """
    try:
        return load_config(Path(path))
    except FileNotFoundError:
        raise SystemExit(f"config file not found: {path}") from None
    except ValidationError as error:
        raise SystemExit(f"{path} is invalid:\n{_validation_summary(error)}") from None


def read_constants(path: str) -> Constants:
    """Load constants.yml, exiting with a readable message when it is invalid.

    Args:
        path: Location of constants.yml.

    Returns:
        Constants: Validated constants.
    """
    try:
        return load_constants(Path(path))
    except FileNotFoundError:
        raise SystemExit(f"constants file not found: {path}") from None
    except ValidationError as error:
        raise SystemExit(f"{path} is invalid:\n{_validation_summary(error)}") from None


def small_pool(config: DatabaseConfig, size: int = SCRIPT_POOL_SIZE) -> DatabaseConfig:
    """Copy database settings with a pool sized for a one-off script.

    Args:
        config: Database settings from config.yml.
        size: Largest number of connections.

    Returns:
        DatabaseConfig: Settings with a small pool.
    """
    return config.model_copy(update={"pool_min_size": 1, "pool_max_size": size})


async def connect(config: DatabaseConfig) -> asyncpg.Connection:
    """Open a single database connection.

    Args:
        config: Database settings.

    Returns:
        asyncpg.Connection: The connection.
    """
    return await asyncpg.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        ssl=config.ssl,
    )


@asynccontextmanager
async def open_context(config: Config, constants: Constants) -> AsyncGenerator[AppContext, None]:
    """Build an AppContext like an API worker does, without the background worker.

    Args:
        config: Validated config.yml.
        constants: Validated constants.yml.

    Returns:
        AsyncGenerator[AppContext, None]: Context yielding the app context.
    """
    database = Database()
    await database.open(small_pool(config.database))
    storage = Storage(config.storage)
    await storage.open()
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    discord = DiscordBot(
        config.discord, http, config.sonolus.server_url, config.sonolus.chart_prefix
    )
    context = AppContext(
        config=config,
        constants=constants,
        database=database,
        storage=storage,
        http=http,
        discord=discord,
        cipher=TokenCipher(config.keys.encryption_key),
        sonolus_key=load_sonolus_key(),
        executor=ThreadPoolExecutor(max_workers=2),
        conversion_children=ConversionChildren(),
        audio_jobs=asyncio.Semaphore(1),
    )
    try:
        yield context
    finally:
        context.conversion_children.kill_all()
        await discord.drain()
        await http.close()
        await storage.close()
        await database.close()
        context.executor.shutdown(wait=False)


async def record_admin_action(
    context: AppContext,
    connection: Connection,
    action: str,
    target_type: str,
    target_id: str,
    previous_value: str | None,
    new_value: str | None,
) -> None:
    """Log a staff action as the admin key, exactly like an admin key API call would.

    Args:
        context: App context.
        connection: Database connection, ideally the transaction making the change.
        action: Action name.
        target_type: Kind of target.
        target_id: Target id.
        previous_value: Value before the action, used for rollback.
        new_value: Value after the action.
    """
    await staff_log.record(
        context, connection, ADMIN_KEY, action, target_type, target_id, previous_value, new_value
    )


async def find_account(
    connection: Connection, account_id: str | None, handle: int | None
) -> AccountRow | None:
    """Look up an account by Sonolus id or handle.

    Args:
        connection: Database connection.
        account_id: Sonolus id, or None to use the handle.
        handle: Sonolus handle, used when no id is given.

    Returns:
        AccountRow | None: The account, or None.
    """
    if account_id is not None:
        return await accounts_db.get_account(connection, account_id)
    if handle is not None:
        return await accounts_db.get_account_by_handle(connection, handle)
    return None


def describe_target(account_id: str | None, handle: int | None) -> str:
    """Describe an account lookup for error messages.

    Args:
        account_id: Sonolus id, if given.
        handle: Sonolus handle, if given.

    Returns:
        str: Text like ``handle #123`` or ``sonolus_id abc``.
    """
    return f"sonolus_id {account_id}" if account_id is not None else f"handle #{handle}"


def account_label(account: AccountRow) -> str:
    """Format an account for output.

    Args:
        account: The account.

    Returns:
        str: Text like ``name#handle (sonolus id)``.
    """
    return f"{account.sonolus_username}#{account.sonolus_handle} ({account.sonolus_id})"


def confirm(prompt: str, expected: str) -> bool:
    """Ask the operator to type an exact answer.

    Args:
        prompt: Question shown before the input.
        expected: Answer that confirms, compared case-insensitively.

    Returns:
        bool: Whether the operator typed the expected answer.
    """
    try:
        answer = input(f"{prompt} ")
    except EOFError:
        return False
    return answer.strip().lower() == expected.lower()


def add_pg_bin_argument(parser: argparse.ArgumentParser) -> None:
    """Add the --pg-bin option to a parser.

    Args:
        parser: Parser to extend.
    """
    parser.add_argument(
        "--pg-bin", help="folder holding pg_dump, pg_restore and psql (default: search PATH)"
    )


def find_pg_tool(name: str, directory: str | None) -> str:
    """Locate a PostgreSQL client program.

    Args:
        name: Program name, such as ``pg_dump``.
        directory: Folder to look in, or None to search PATH.

    Returns:
        str: Path of the program.
    """
    found = shutil.which(name, path=directory) if directory else shutil.which(name)
    if found is None:
        where = f"in {directory}" if directory else "on PATH"
        raise SystemExit(
            f"{name} not found {where}; install the PostgreSQL client or pass --pg-bin"
        )
    return found


def pg_environment(config: DatabaseConfig) -> dict[str, str]:
    """Build the environment for PostgreSQL client programs, password included.

    Args:
        config: Database settings.

    Returns:
        dict[str, str]: Environment variables for the child process.
    """
    environment = os.environ.copy()
    environment["PGPASSWORD"] = config.password
    environment["PGSSLMODE"] = config.ssl
    return environment


def pg_connection_arguments(config: DatabaseConfig) -> list[str]:
    """Build the connection options shared by pg_dump, pg_restore and psql.

    Args:
        config: Database settings.

    Returns:
        list[str]: Host, port, user and database options.
    """
    return ["-h", config.host, "-p", str(config.port), "-U", config.user, "-d", config.database]


def format_bytes(size: int) -> str:
    """Format a byte count for humans.

    Args:
        size: Number of bytes.

    Returns:
        str: Text like ``12.3 MiB``.
    """
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
