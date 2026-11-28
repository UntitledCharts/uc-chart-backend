import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Literal

import asyncpg

from app.database import accounts as accounts_db
from app.database import collaborators as collaborators_db
from app.database import permissions as permissions_db
from app.services.account_collaborations import RESTORED_VALUE
from scripts._common import add_config_argument, connect, read_config

Outcome = Literal["rolled_back", "no_op", "skipped"]

ROLLBACK_HANDLERS: dict[str, str] = {
    "visibility_change": """
        WITH synced AS (
            UPDATE leaderboards SET public_chart = ($1::chart_status = 'PUBLIC')
            WHERE chart_id = $2
              AND EXISTS (
                  SELECT 1 FROM charts WHERE id = $2 AND status IS DISTINCT FROM $1::chart_status
              )
        )
        UPDATE charts SET status = $1::chart_status, updated_at = CURRENT_TIMESTAMP
        WHERE id = $2 AND status IS DISTINCT FROM $1::chart_status;
    """,
    "comment_delete": """
        UPDATE comments SET deleted_at = NULL WHERE id = $1::int AND deleted_at IS NOT NULL;
    """,
    "staff_pick": """
        UPDATE charts SET staff_pick = $1::bool, updated_at = CURRENT_TIMESTAMP
        WHERE id = $2 AND COALESCE(staff_pick, false) IS DISTINCT FROM $1::bool;
    """,
    "constant_rerate": """
        UPDATE charts SET rating = $1::decimal, updated_at = CURRENT_TIMESTAMP
        WHERE id = $2 AND rating IS DISTINCT FROM $1::decimal;
    """,
    "ban": """
        UPDATE accounts SET banned = $1::bool, deleted_at = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE sonolus_id = $2
          AND (COALESCE(banned, false) IS DISTINCT FROM $1::bool OR deleted_at IS NOT NULL);
    """,
    "delete": """
        UPDATE charts SET deleted_at = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE id = $1 AND deleted_at IS NOT NULL;
    """,
    "undelete": """
        UPDATE charts SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = $1 AND deleted_at IS NULL;
    """,
}

_TARGET_ONLY_ACTIONS = {"comment_delete", "delete", "undelete"}
_BOOL_VALUE_ACTIONS = {"ban", "staff_pick"}
_INT_TARGET_ACTIONS = {"comment_delete"}
IRREVERSIBLE_ACTIONS = {"account_delete", "leaderboard_delete", "chart_purge"}

RollbackFunction = Callable[[asyncpg.Connection, asyncpg.Record], Awaitable[Outcome]]


def _as_bool(value: str | None) -> bool | None:
    """Turn a logged ``True``/``False`` value into a bool for a ``::bool`` parameter.

    Args:
        value: Logged value.

    Returns:
        bool | None: The flag, or None when nothing was logged.
    """
    if value is None:
        return None
    return value.strip().lower() in ("true", "t", "1", "yes")


def _shorten(value: str | None, limit: int = 120) -> str | None:
    """Cut a logged value down for the action list.

    Args:
        value: Logged value.
        limit: Most characters shown.

    Returns:
        str | None: The value, cut with an ellipsis when longer than the limit.
    """
    if value is None or len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _rows_changed(status: str) -> int:
    """Read the row count from a command status such as ``UPDATE 1``.

    Args:
        status: Command status returned by ``execute``.

    Returns:
        int: Rows the command changed.
    """
    last = status.rsplit(" ", 1)[-1]
    return int(last) if last.isdigit() else 0


def _outcome(changed: int | bool) -> Outcome:
    """Turn a changed-row count into an outcome.

    Args:
        changed: Rows changed, or whether a row changed.

    Returns:
        Outcome: ``rolled_back`` when something changed, otherwise ``no_op``.
    """
    return "rolled_back" if changed else "no_op"


async def _rollback_permission_grant(
    connection: asyncpg.Connection, row: asyncpg.Record
) -> Outcome:
    """Revoke a permission that was granted.

    Args:
        connection: Database connection inside a transaction.
        row: The permission_grant log entry.

    Returns:
        Outcome: What the rollback did.
    """
    if not row["new_value"]:
        print(f"  [SKIP] permission_grant #{row['id']} names no permission")
        return "skipped"
    return _outcome(
        await permissions_db.revoke_permission(connection, row["target_id"], row["new_value"])
    )


async def _rollback_permission_revoke(
    connection: asyncpg.Connection, row: asyncpg.Record
) -> Outcome:
    """Grant back a permission that was revoked.

    Args:
        connection: Database connection inside a transaction.
        row: The permission_revoke log entry.

    Returns:
        Outcome: What the rollback did.
    """
    if not row["previous_value"]:
        print(f"  [SKIP] permission_revoke #{row['id']} names no permission")
        return "skipped"
    if await accounts_db.get_account(connection, row["target_id"]) is None:
        print(
            f"  [SKIP] permission_revoke #{row['id']}: account {row['target_id']} no longer exists"
        )
        return "skipped"
    return _outcome(
        await permissions_db.grant_permission(
            connection, row["target_id"], row["previous_value"], None
        )
    )


async def _rollback_schedule_change(connection: asyncpg.Connection, row: asyncpg.Record) -> Outcome:
    """Restore a chart's previous scheduled publish time while it is still ahead, or clear it.

    Args:
        connection: Database connection inside a transaction.
        row: The schedule_change log entry.

    Returns:
        Outcome: What the rollback did.
    """
    previous = row["previous_value"]
    publish_at = datetime.fromisoformat(previous) if previous else None
    if publish_at is not None and publish_at.tzinfo is None:
        publish_at = publish_at.replace(tzinfo=timezone.utc)
    if publish_at is not None and publish_at <= datetime.now(timezone.utc):
        print(
            f"  [NOTE] schedule_change #{row['id']}: the previous publish time {previous} has "
            "passed, so the schedule is cleared instead of publishing the chart right away"
        )
        publish_at = None
    status = await connection.execute(
        """
        UPDATE charts SET scheduled_publish = $2, updated_at = CURRENT_TIMESTAMP
        WHERE id = $1 AND scheduled_publish IS DISTINCT FROM $2
        """,
        row["target_id"],
        publish_at,
    )
    return _outcome(_rows_changed(status))


async def _rollback_collaborations_removed(
    connection: asyncpg.Connection, row: asyncpg.Record
) -> Outcome:
    """Put back the collaborations and invites a ban removed, unless an unban already did.

    Args:
        connection: Database connection inside a transaction.
        row: The collaborations_removed log entry.

    Returns:
        Outcome: What the rollback did.
    """
    if row["new_value"] == RESTORED_VALUE or not row["previous_value"]:
        return "no_op"
    if await accounts_db.get_account(connection, row["target_id"]) is None:
        print(
            f"  [SKIP] collaborations_removed #{row['id']}: account {row['target_id']} "
            "no longer exists"
        )
        return "skipped"
    await collaborators_db.restore_for_account(connection, row["target_id"], row["previous_value"])
    return "rolled_back"


def _badge_value(value: str | None) -> tuple[str, int] | None:
    """Split a logged ``badge:level`` value.

    Args:
        value: Logged value.

    Returns:
        tuple[str, int] | None: Badge name and level, or None when nothing usable was logged.
    """
    if not value or ":" not in value:
        return None
    badge, level = value.rsplit(":", 1)
    return (badge, int(level)) if level.isdigit() else None


async def _rollback_badge_grant(connection: asyncpg.Connection, row: asyncpg.Record) -> Outcome:
    """Take back a granted badge, or put back its previous level.

    Args:
        connection: Database connection inside a transaction.
        row: The badge_grant log entry.

    Returns:
        Outcome: What the rollback did.
    """
    granted = _badge_value(row["new_value"])
    if granted is None:
        print(f"  [SKIP] badge_grant #{row['id']} names no badge")
        return "skipped"
    badge, level = granted
    previous = _badge_value(row["previous_value"])
    if previous is None:
        status = await connection.execute(
            "DELETE FROM account_badges WHERE sonolus_id = $1 AND badge = $2 AND level = $3",
            row["target_id"],
            badge,
            level,
        )
    else:
        status = await connection.execute(
            "UPDATE account_badges SET level = $4 WHERE sonolus_id = $1 AND badge = $2 AND level = $3",
            row["target_id"],
            badge,
            level,
            previous[1],
        )
    return _outcome(_rows_changed(status))


async def _rollback_badge_revoke(connection: asyncpg.Connection, row: asyncpg.Record) -> Outcome:
    """Give back a removed badge at its old level.

    Args:
        connection: Database connection inside a transaction.
        row: The badge_revoke log entry.

    Returns:
        Outcome: What the rollback did.
    """
    removed = _badge_value(row["previous_value"])
    if removed is None:
        print(f"  [SKIP] badge_revoke #{row['id']} names no badge")
        return "skipped"
    if await accounts_db.get_account(connection, row["target_id"]) is None:
        print(f"  [SKIP] badge_revoke #{row['id']}: account {row['target_id']} no longer exists")
        return "skipped"
    status = await connection.execute(
        """
        INSERT INTO account_badges (sonolus_id, badge, level) VALUES ($1, $2, $3)
        ON CONFLICT (sonolus_id, badge) DO NOTHING
        """,
        row["target_id"],
        removed[0],
        removed[1],
    )
    return _outcome(_rows_changed(status))


FUNCTION_HANDLERS: dict[str, RollbackFunction] = {
    "badge_grant": _rollback_badge_grant,
    "badge_revoke": _rollback_badge_revoke,
    "permission_grant": _rollback_permission_grant,
    "permission_revoke": _rollback_permission_revoke,
    "schedule_change": _rollback_schedule_change,
    "collaborations_removed": _rollback_collaborations_removed,
}


async def rollback_action(connection: asyncpg.Connection, row: asyncpg.Record) -> Outcome:
    """Undo one logged staff action.

    Args:
        connection: Database connection inside a transaction.
        row: The log entry.

    Returns:
        Outcome: ``rolled_back`` when a row changed and the log entry can be removed, ``no_op``
            when the target is gone or already holds the old value, ``skipped`` when the action
            can't be rolled back.
    """
    action = row["action"]
    if action in IRREVERSIBLE_ACTIONS:
        print(f"  [SKIP] not reversible: {action} {row['target_type']}:{row['target_id']}")
        return "skipped"
    function = FUNCTION_HANDLERS.get(action)
    if function is not None:
        return await function(connection, row)
    handler = ROLLBACK_HANDLERS.get(action)
    if not handler:
        print(f"  [SKIP] unknown action: {action}")
        return "skipped"

    target = int(row["target_id"]) if action in _INT_TARGET_ACTIONS else row["target_id"]
    if action in _TARGET_ONLY_ACTIONS:
        status = await connection.execute(handler, target)
    else:
        previous = (
            _as_bool(row["previous_value"])
            if action in _BOOL_VALUE_ACTIONS
            else row["previous_value"]
        )
        status = await connection.execute(handler, previous, target)
    return _outcome(_rows_changed(status))


def parse_since(value: str) -> datetime:
    """Parse --since, reading naive timestamps as UTC.

    Args:
        value: ISO timestamp.

    Returns:
        datetime: Aware timestamp.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {value}") from None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


async def run(args: argparse.Namespace) -> None:
    """List and optionally roll back staff actions.

    Args:
        args: Parsed command line.
    """
    config = read_config(args.config)
    connection = await connect(config.database)

    try:
        since_ts: datetime = args.since
        select = """
            SELECT sa.*, a.sonolus_username, a.sonolus_handle
            FROM staff_actions sa
            LEFT JOIN accounts a ON sa.actor_id = a.sonolus_id
        """
        if args.admin_key:
            who = "the admin key"
            rows = await connection.fetch(
                select
                + """
                WHERE sa.created_at >= $1 AND sa.via_admin_key AND sa.actor_id IS NULL
                ORDER BY sa.created_at DESC, sa.id DESC;
                """,
                since_ts,
            )
        else:
            who = f"#{args.handle}"
            actor = await accounts_db.get_account_by_handle(connection, args.handle)
            if actor is None:
                print(f"No account found for handle #{args.handle}.")
                return
            rows = await connection.fetch(
                select
                + """
                WHERE sa.created_at >= $1 AND sa.actor_id = $2
                ORDER BY sa.created_at DESC, sa.id DESC;
                """,
                since_ts,
                actor.sonolus_id,
            )

        if not rows:
            print(f"No staff actions found for {who} since {args.since.isoformat()}.")
            return

        print(f"Found {len(rows)} action(s) by {who} since {args.since.isoformat()}:\n")
        skipped = 0
        for i, row in enumerate(rows):
            if row["via_admin_key"]:
                actor_name = "admin key"
            elif row["sonolus_username"]:
                actor_name = f"{row['sonolus_username']}#{row['sonolus_handle']}"
            else:
                actor_name = row["actor_id"]
            note = ""
            if row["action"] in IRREVERSIBLE_ACTIONS:
                note = " [not reversible, skipped]"
                skipped += 1
            print(
                f"  [{i}] {row['created_at']} | {actor_name} | {row['action']} "
                f"| {row['target_type']}:{row['target_id']} "
                f"| {_shorten(row['previous_value'])} -> {_shorten(row['new_value'])}{note}"
            )
        if skipped:
            print(f"\n{skipped} action(s) can't be reversed and will be skipped.")

        if args.dry_run:
            print("\n(dry run, no changes made)")
            return

        print()
        confirm = input("Rollback ALL listed actions? (y/N): ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

        rolled_back = 0
        no_ops = 0
        for row in rows:
            try:
                async with connection.transaction():
                    outcome = await rollback_action(connection, row)
                    if outcome == "rolled_back":
                        await connection.execute(
                            "DELETE FROM staff_actions WHERE id = $1;", row["id"]
                        )
            except (asyncpg.PostgresError, asyncpg.InterfaceError, ValueError) as error:
                print(
                    f"  [FAIL] {row['action']} {row['target_type']}:{row['target_id']}: {error}\n"
                    "  Stopping here; actions rolled back so far stay rolled back."
                )
                break
            if outcome == "rolled_back":
                rolled_back += 1
            elif outcome == "no_op":
                no_ops += 1
                print(
                    f"  [NO-OP] {row['action']} {row['target_type']}:{row['target_id']}: "
                    "the target is gone or already has the old value, so its log entry is kept"
                )

        print(f"\nRolled back {rolled_back}/{len(rows)} action(s), {no_ops} no-op(s).")
    finally:
        await connection.close()


def main() -> None:
    """Parse the command line and run the rollback."""
    p = argparse.ArgumentParser(description="Rollback staff actions since a timestamp")
    add_config_argument(p)
    actor = p.add_mutually_exclusive_group(required=True)
    actor.add_argument(
        "--handle",
        type=int,
        help="Sonolus handle of the mod whose actions to rollback",
    )
    actor.add_argument(
        "--admin-key",
        action="store_true",
        help="rollback actions done with the admin key (API calls and scripts) instead",
    )
    p.add_argument(
        "--since",
        required=True,
        type=parse_since,
        help="ISO timestamp to rollback from, UTC unless it has an offset (e.g. 2026-04-11T00:00:00)",
    )
    p.add_argument("--dry-run", action="store_true", help="List actions without rolling back")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
