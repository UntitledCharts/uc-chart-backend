from datetime import datetime

from pydantic import BaseModel

from app.database.core import Connection, QueryBuilder, fetch_all, fetch_one


class StaffActionRow(BaseModel):
    """A logged staff action.

    Attributes:
        id: Log entry id.
        actor_id: Acting account, or None for the admin key or a deleted account.
        via_admin_key: Whether the admin key performed the action.
        action: Action name.
        target_type: Kind of target, such as chart or account.
        target_id: Target id.
        previous_value: Value before the action, used for rollback.
        new_value: Value after the action.
        created_at: Time of the action.
    """

    id: int
    actor_id: str | None = None
    via_admin_key: bool = False
    action: str
    target_type: str
    target_id: str
    previous_value: str | None = None
    new_value: str | None = None
    created_at: datetime


async def log_action(
    connection: Connection,
    actor_id: str | None,
    via_admin_key: bool,
    action: str,
    target_type: str,
    target_id: str,
    previous_value: str | None,
    new_value: str | None,
) -> StaffActionRow:
    """Record a staff action.

    Args:
        connection: Database connection.
        actor_id: Acting account, or None for the admin key.
        via_admin_key: Whether the admin key performed the action.
        action: Action name.
        target_type: Kind of target.
        target_id: Target id.
        previous_value: Value before the action.
        new_value: Value after the action.

    Returns:
        StaffActionRow: The log entry.
    """
    row = await fetch_one(
        connection,
        StaffActionRow,
        """
        INSERT INTO staff_actions (actor_id, via_admin_key, action, target_type, target_id, previous_value, new_value)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id, actor_id, via_admin_key, action, target_type, target_id, previous_value, new_value, created_at
        """,
        actor_id,
        via_admin_key,
        action,
        target_type,
        target_id,
        previous_value,
        new_value,
    )
    assert row is not None
    return row


async def list_actions(
    connection: Connection,
    page: int,
    page_size: int,
    actor_id: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[list[StaffActionRow], int]:
    """List staff actions, newest first.

    Args:
        connection: Database connection.
        page: Zero-based page.
        page_size: Entries per page.
        actor_id: Only actions by this account.
        action: Only this action name.
        target_type: Only this kind of target.
        target_id: Only this target.
        since: Only actions at or after this time.
        until: Only actions before this time.

    Returns:
        tuple[list[StaffActionRow], int]: The page and the total number of matching entries.
    """
    query = QueryBuilder()
    if actor_id is not None:
        query.where(f"actor_id = {query.param(actor_id)}")
    if action is not None:
        query.where(f"action = {query.param(action)}")
    if target_type is not None:
        query.where(f"target_type = {query.param(target_type)}")
    if target_id is not None:
        query.where(f"target_id = {query.param(target_id)}")
    if since is not None:
        query.where(f"created_at >= {query.param(since)}")
    if until is not None:
        query.where(f"created_at < {query.param(until)}")
    where = query.where_clause()
    total = await connection.fetchval(f"SELECT COUNT(*) FROM staff_actions {where}", *query.params)
    limit = query.param(page_size)
    offset = query.param(page * page_size)
    rows = await fetch_all(
        connection,
        StaffActionRow,
        f"""
        SELECT id, actor_id, via_admin_key, action, target_type, target_id, previous_value, new_value, created_at
        FROM staff_actions {where}
        ORDER BY created_at DESC, id DESC
        LIMIT {limit} OFFSET {offset}
        """,
        *query.params,
    )
    return rows, int(total or 0)
