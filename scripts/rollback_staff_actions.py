import argparse
import asyncio
from datetime import datetime, timezone

import asyncpg
import yaml

ROLLBACK_HANDLERS: dict[str, str] = {
    "visibility_change": """
        UPDATE charts SET status = $1::chart_status, updated_at = CURRENT_TIMESTAMP
        WHERE id = $2;
    """,
    "comment_delete": """
        UPDATE comments SET deleted_at = NULL WHERE id = $1::int;
    """,
    "staff_pick": """
        UPDATE charts SET staff_pick = $1::bool, updated_at = CURRENT_TIMESTAMP
        WHERE id = $2;
    """,
    "constant_rerate": """
        UPDATE charts SET rating = $1::decimal, updated_at = CURRENT_TIMESTAMP
        WHERE id = $2;
    """,
    # only restores the flag, charts deleted along with the ban stay deleted
    "ban": """
        UPDATE accounts SET banned = $1::bool, updated_at = CURRENT_TIMESTAMP
        WHERE sonolus_id = $2;
    """,
}


async def rollback_action(conn: asyncpg.Connection, row: asyncpg.Record) -> bool:
    action = row["action"]
    handler = ROLLBACK_HANDLERS.get(action)
    if not handler:
        print(f"  [SKIP] unknown action: {action}")
        return False

    if action == "comment_delete":
        await conn.execute(handler, row["target_id"])
    else:
        await conn.execute(handler, row["previous_value"], row["target_id"])

    return True


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
        since_ts = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

        actor_id = await conn.fetchval(
            "SELECT sonolus_id FROM accounts WHERE sonolus_handle = $1;",
            args.handle,
        )
        if not actor_id:
            print(f"No account found for handle #{args.handle}.")
            return

        rows = await conn.fetch(
            """
            SELECT sa.*, a.sonolus_username, a.sonolus_handle
            FROM staff_actions sa
            LEFT JOIN accounts a ON sa.actor_id = a.sonolus_id
            WHERE sa.created_at >= $1 AND sa.actor_id = $2
            ORDER BY sa.created_at DESC;
            """,
            since_ts,
            actor_id,
        )

        if not rows:
            print(
                f"No staff actions found for handle #{args.handle} since {args.since}."
            )
            return

        print(f"Found {len(rows)} action(s) by #{args.handle} since {args.since}:\n")
        for i, row in enumerate(rows):
            actor = (
                f"{row['sonolus_username']}#{row['sonolus_handle']}"
                if row["sonolus_username"]
                else row["actor_id"]
            )
            print(
                f"  [{i}] {row['created_at']} | {actor} | {row['action']} "
                f"| {row['target_type']}:{row['target_id']} "
                f"| {row['previous_value']} -> {row['new_value']}"
            )

        if args.dry_run:
            print("\n(dry run, no changes made)")
            return

        print()
        confirm = input("Rollback ALL listed actions? (y/N): ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

        rolled_back = 0
        for row in rows:
            ok = await rollback_action(conn, row)
            if ok:
                await conn.execute(
                    "DELETE FROM staff_actions WHERE id = $1;", row["id"]
                )
                rolled_back += 1

        print(f"\nRolled back {rolled_back}/{len(rows)} action(s).")
    finally:
        await conn.close()


def main():
    p = argparse.ArgumentParser(description="Rollback staff actions since a timestamp")
    p.add_argument(
        "--handle",
        type=int,
        required=True,
        help="Sonolus handle of the mod whose actions to rollback",
    )
    p.add_argument(
        "--since",
        required=True,
        help="ISO timestamp to rollback from (e.g. 2026-04-11T00:00:00)",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="List actions without rolling back"
    )
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
