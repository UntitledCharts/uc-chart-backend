from app.auth.principal import Principal
from app.database import collaborators as collaborators_db
from app.database import staff_actions as staff_actions_db
from app.database.core import Connection

REMOVED_ACTION = "collaborations_removed"
RESTORED_VALUE = "restored"


async def remove_all(connection: Connection, principal: Principal, sonolus_id: str) -> None:
    """Remove an account's collaborations and invites, logging them so they can be restored.

    Args:
        connection: Database connection inside the transaction banning the account.
        principal: Staff member or admin key banning the account.
        sonolus_id: Account id.
    """
    snapshot = await collaborators_db.take_all_for_account(connection, sonolus_id)
    if snapshot is None:
        return
    await staff_actions_db.log_action(
        connection,
        principal.sonolus_id,
        principal.is_admin_key,
        REMOVED_ACTION,
        "account",
        sonolus_id,
        snapshot,
        None,
    )


async def restore_all(connection: Connection, sonolus_id: str) -> int:
    """Restore every collaboration and invite removed from an account and not restored yet.

    Args:
        connection: Database connection inside the transaction unbanning the account.
        sonolus_id: Account id.

    Returns:
        int: Collaborations and invites restored.
    """
    entries = await connection.fetch(
        """
        SELECT id, previous_value FROM staff_actions
        WHERE action = $1 AND target_type = 'account' AND target_id = $2 AND new_value IS NULL
        ORDER BY id
        FOR UPDATE
        """,
        REMOVED_ACTION,
        sonolus_id,
    )
    restored = 0
    for entry in entries:
        if entry["previous_value"]:
            restored += await collaborators_db.restore_for_account(
                connection, sonolus_id, entry["previous_value"]
            )
    if entries:
        await connection.execute(
            "UPDATE staff_actions SET new_value = $2 WHERE id = ANY($1::int[])",
            [entry["id"] for entry in entries],
            RESTORED_VALUE,
        )
    return restored
