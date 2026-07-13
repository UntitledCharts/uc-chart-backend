import argparse
import asyncio
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
import yaml

from database import DBConnWrapper, oauth
from helpers.oauth import generate_client_id, generate_client_secret, hash_token


async def get_pool() -> asyncpg.Pool:
    with open("config.yml", "r") as file:
        config = yaml.safe_load(file)

    psql = config["psql"]
    return await asyncpg.create_pool(
        host=psql["host"],
        user=psql["user"],
        database=psql["database"],
        password=psql["password"],
        port=psql["port"],
        min_size=1,
        max_size=1,
        ssl="disable",
    )


async def create(
    name: str, redirect_uris: list[str], description: str | None, owner: str | None
) -> None:
    client_id = generate_client_id()
    client_secret = generate_client_secret()

    pool = await get_pool()
    async with pool.acquire() as connection:
        conn = DBConnWrapper(connection)
        app = await conn.fetchrow(
            oauth.create_app(
                client_id=client_id,
                client_secret_hash=hash_token(client_secret),
                name=name,
                redirect_uris=redirect_uris,
                description=description,
                owner_id=owner,
            )
        )
    await pool.close()

    print(f"name:          {app.name}")
    print(f"description:   {app.description or '-'}")
    print(f"client_id:     {app.client_id}")
    print(f"client_secret: {client_secret}")
    print(f"redirect_uris: {', '.join(app.redirect_uris)}")
    print("\nThe secret is only shown once, it is stored hashed.")


async def regenerate(client_id: str) -> None:
    client_secret = generate_client_secret()

    pool = await get_pool()
    async with pool.acquire() as connection:
        conn = DBConnWrapper(connection)
        app = await conn.fetchrow(
            oauth.regenerate_secret(
                client_id=client_id, client_secret_hash=hash_token(client_secret)
            )
        )
    await pool.close()

    if not app:
        raise SystemExit(f"No app with client_id {client_id}.")

    print(f"name:          {app.name}")
    print(f"client_id:     {app.client_id}")
    print(f"client_secret: {client_secret}")
    print("\nThe old secret no longer works. Existing tokens are unaffected.")


async def set_redirect_uris(client_id: str, redirect_uris: list[str]) -> None:
    pool = await get_pool()
    async with pool.acquire() as connection:
        conn = DBConnWrapper(connection)
        app = await conn.fetchrow(
            oauth.set_redirect_uris(client_id=client_id, redirect_uris=redirect_uris)
        )
    await pool.close()

    if not app:
        raise SystemExit(f"No app with client_id {client_id}.")

    print(f"{app.name}: {', '.join(app.redirect_uris)}")


async def delete(client_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as connection:
        conn = DBConnWrapper(connection)
        app = await conn.fetchrow(oauth.delete_app(client_id))
    await pool.close()

    if not app:
        raise SystemExit(f"No app with client_id {client_id}.")

    print(f"Deleted {app.name} ({app.client_id}). Its tokens are gone too.")


async def apps() -> None:
    pool = await get_pool()
    async with pool.acquire() as connection:
        conn = DBConnWrapper(connection)
        result = await conn.fetch(oauth.list_apps())
    await pool.close()

    if not result:
        print("No apps.")
        return

    for app in result:
        print(f"{app.client_id}  {app.name}  {', '.join(app.redirect_uris)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage OAuth apps.")
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create", help="create an app")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument(
        "--redirect-uri", required=True, action="append", dest="redirect_uris"
    )
    create_parser.add_argument(
        "--description", help="shown to users on the consent screen"
    )
    create_parser.add_argument("--owner", help="sonolus_id of the app owner")

    regenerate_parser = sub.add_parser("regenerate", help="regenerate a client secret")
    regenerate_parser.add_argument("--client-id", required=True)

    redirects_parser = sub.add_parser(
        "redirect-uris", help="replace the redirect uris of an app"
    )
    redirects_parser.add_argument("--client-id", required=True)
    redirects_parser.add_argument(
        "--redirect-uri", required=True, action="append", dest="redirect_uris"
    )

    delete_parser = sub.add_parser("delete", help="delete an app")
    delete_parser.add_argument("--client-id", required=True)

    sub.add_parser("list", help="list all apps")

    args = parser.parse_args()

    if args.command == "create":
        asyncio.run(create(args.name, args.redirect_uris, args.description, args.owner))
    elif args.command == "regenerate":
        asyncio.run(regenerate(args.client_id))
    elif args.command == "redirect-uris":
        asyncio.run(set_redirect_uris(args.client_id, args.redirect_uris))
    elif args.command == "delete":
        asyncio.run(delete(args.client_id))
    elif args.command == "list":
        asyncio.run(apps())


if __name__ == "__main__":
    main()
