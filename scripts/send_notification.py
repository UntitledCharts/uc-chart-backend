import argparse
import asyncio

import asyncpg
import yaml


async def run(args):
    with open("config.yml", "r") as f:
        config = yaml.safe_load(f)

    psql = config["psql"]
    conn = await asyncpg.connect(
        host=psql["host"],
        user=psql["user"],
        database=psql["database"],
        password=psql["password"],
        port=psql["port"],
        ssl="disable",
    )

    try:
        if args.handle is not None:
            user_id = await conn.fetchval(
                "SELECT sonolus_id FROM accounts WHERE sonolus_handle = $1;",
                args.handle,
            )
            if not user_id:
                print(f"No account found for handle #{args.handle}.")
                return
        else:
            user_id = args.id
            exists = await conn.fetchval(
                "SELECT 1 FROM accounts WHERE sonolus_id = $1;", user_id
            )
            if not exists:
                print(f"No account found for sonolus_id {user_id}.")
                return

        row = await conn.fetchrow(
            """
            INSERT INTO notifications (user_id, title, content)
            VALUES ($1, $2, $3)
            RETURNING id;
            """,
            user_id,
            args.title,
            args.content,
        )

        print(f"Sent notification #{row['id']} to {user_id}.")
    finally:
        await conn.close()


def main():
    p = argparse.ArgumentParser(description="Send a custom notification to a user")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", help="Sonolus ID of the recipient")
    target.add_argument("--handle", type=int, help="Sonolus handle of the recipient")
    p.add_argument("--title", required=True, help="Notification title")
    p.add_argument("--content", default=None, help="Notification body")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
