import argparse
import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from urllib.parse import urlsplit

import asyncpg

from app.auth.tokens import (
    CLIENT_SECRET_PREFIX,
    generate_client_id,
    generate_token,
    hash_token,
)
from app.database import accounts as accounts_db
from app.database import oauth as oauth_db
from scripts._common import add_config_argument, confirm, connect, read_config


def check_redirect_uris(parser: argparse.ArgumentParser, redirect_uris: list[str]) -> list[str]:
    """Reject redirect URIs that can't work, and drop duplicates.

    Args:
        parser: Parser used to report bad input.
        redirect_uris: URIs from the command line.

    Returns:
        list[str]: The URIs in order without duplicates.
    """
    for uri in redirect_uris:
        parts = urlsplit(uri)
        if (
            not parts.scheme
            or parts.fragment
            or (parts.scheme in ("http", "https") and not parts.netloc)
        ):
            parser.error(f"not a usable redirect uri: {uri}")
    return list(dict.fromkeys(redirect_uris))


async def create(
    conn: asyncpg.Connection,
    name: str,
    redirect_uris: list[str],
    description: str | None,
    public: bool,
    owner: str | None,
) -> None:
    """Register an app and print its credentials.

    Args:
        conn: Database connection.
        name: App name.
        redirect_uris: Allowed redirect URIs.
        description: Text shown on the consent screen.
        public: Whether the app can't keep a secret.
        owner: Owning account's sonolus_id.
    """
    if owner is not None and await accounts_db.get_account(conn, owner) is None:
        raise SystemExit(f"No account with sonolus_id {owner}.")
    client_id = generate_client_id()
    client_secret = None if public else generate_token(CLIENT_SECRET_PREFIX)
    app = await oauth_db.create_app(
        conn,
        client_id=client_id,
        client_secret_hash=hash_token(client_secret) if client_secret else None,
        name=name,
        redirect_uris=redirect_uris,
        description=description,
        public=public,
        owner_id=owner,
    )

    print(f"name:          {app.name}")
    print(f"description:   {app.description or '-'}")
    print(f"type:          {'public (no secret, PKCE required)' if app.public else 'confidential'}")
    print(f"client_id:     {app.client_id}")
    if client_secret:
        print(f"client_secret: {client_secret}")
    print(f"redirect_uris: {', '.join(app.redirect_uris)}")
    if client_secret:
        print("\nThe secret is only shown once, it is stored hashed.")


async def regenerate(conn: asyncpg.Connection, client_id: str) -> None:
    """Replace a confidential app's client secret and print it.

    Args:
        conn: Database connection.
        client_id: Client id.
    """
    existing = await oauth_db.get_app(conn, client_id)
    if not existing:
        raise SystemExit(f"No app with client_id {client_id}.")
    if existing.public:
        raise SystemExit(f"{existing.name} is a public client: it has no secret.")

    client_secret = generate_token(CLIENT_SECRET_PREFIX)
    app = await oauth_db.set_client_secret(conn, client_id, hash_token(client_secret))
    if app is None:
        raise SystemExit(f"No app with client_id {client_id}.")

    print(f"name:          {app.name}")
    print(f"client_id:     {app.client_id}")
    print(f"client_secret: {client_secret}")
    print("\nThe old secret no longer works. Existing tokens are unaffected.")


async def set_redirect_uris(
    conn: asyncpg.Connection, client_id: str, redirect_uris: list[str]
) -> None:
    """Replace an app's redirect URIs.

    Args:
        conn: Database connection.
        client_id: Client id.
        redirect_uris: New redirect URIs.
    """
    app = await oauth_db.set_redirect_uris(conn, client_id, redirect_uris)
    if not app:
        raise SystemExit(f"No app with client_id {client_id}.")

    print(f"{app.name}: {', '.join(app.redirect_uris)}")


async def delete(conn: asyncpg.Connection, client_id: str, assume_yes: bool) -> None:
    """Delete an app with its codes and tokens.

    Args:
        conn: Database connection.
        client_id: Client id.
        assume_yes: Skip the confirmation prompt.
    """
    app = await oauth_db.get_app(conn, client_id)
    if not app:
        raise SystemExit(f"No app with client_id {client_id}.")
    if not assume_yes and not confirm(
        f"Delete {app.name} ({app.client_id}) and sign out everyone using it? Type 'yes':",
        "yes",
    ):
        print("Aborted.")
        return
    if not await oauth_db.delete_app(conn, client_id):
        raise SystemExit(f"No app with client_id {client_id}.")

    print(f"Deleted {app.name} ({app.client_id}). Its tokens are gone too.")


async def apps(conn: asyncpg.Connection) -> None:
    """Print every app.

    Args:
        conn: Database connection.
    """
    result = await oauth_db.list_apps(conn)
    if not result:
        print("No apps.")
        return

    for app in result:
        kind = "public" if app.public else "confidential"
        print(f"{app.client_id}  {kind:12}  {app.name}  {', '.join(app.redirect_uris)}")


async def run(config_path: str, command: Callable[[asyncpg.Connection], Awaitable[None]]) -> None:
    """Open a connection and run one subcommand on it.

    Args:
        config_path: Location of config.yml.
        command: Subcommand taking the connection.
    """
    config = read_config(config_path)
    conn = await connect(config.database)
    try:
        await command(conn)
    finally:
        await conn.close()


def main() -> None:
    """Parse the command line and run the subcommand."""
    parser = argparse.ArgumentParser(description="Manage OAuth apps.")
    add_config_argument(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create", help="create an app")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument(
        "--redirect-uri", required=True, action="append", dest="redirect_uris"
    )
    create_parser.add_argument("--description", help="shown to users on the consent screen")
    create_parser.add_argument(
        "--public",
        action="store_true",
        help="app can't keep a secret (mobile/CLI/SPA); no secret is issued and PKCE is required",
    )
    create_parser.add_argument("--owner", help="sonolus_id of the app owner")

    regenerate_parser = sub.add_parser("regenerate", help="regenerate a client secret")
    regenerate_parser.add_argument("--client-id", required=True)

    redirects_parser = sub.add_parser("redirect-uris", help="replace the redirect uris of an app")
    redirects_parser.add_argument("--client-id", required=True)
    redirects_parser.add_argument(
        "--redirect-uri", required=True, action="append", dest="redirect_uris"
    )

    delete_parser = sub.add_parser("delete", help="delete an app")
    delete_parser.add_argument("--client-id", required=True)
    delete_parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    sub.add_parser("list", help="list all apps")

    args = parser.parse_args()

    command: Callable[[asyncpg.Connection], Awaitable[None]]
    if args.command == "create":
        command = partial(
            create,
            name=args.name,
            redirect_uris=check_redirect_uris(parser, args.redirect_uris),
            description=args.description,
            public=args.public,
            owner=args.owner,
        )
    elif args.command == "regenerate":
        command = partial(regenerate, client_id=args.client_id)
    elif args.command == "redirect-uris":
        command = partial(
            set_redirect_uris,
            client_id=args.client_id,
            redirect_uris=check_redirect_uris(parser, args.redirect_uris),
        )
    elif args.command == "delete":
        command = partial(delete, client_id=args.client_id, assume_yes=args.yes)
    else:
        command = apps

    asyncio.run(run(args.config, command))


if __name__ == "__main__":
    main()
