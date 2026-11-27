from app.context import AppContext
from app.database import accounts as accounts_db
from app.database import charts as charts_db


async def purge_chart(context: AppContext, chart_id: str, grace_days: int | None = None) -> bool:
    """Delete a chart's row, then its files on a best-effort basis.

    Args:
        context: App context.
        chart_id: Chart id.
        grace_days: Only purge a chart soft-deleted at least this many days ago, None to purge it
            regardless.

    Returns:
        bool: Whether the chart was purged.
    """
    async with context.database.transaction() as connection:
        deleted, owner_id = await charts_db.hard_delete(connection, chart_id, grace_days)
    if not deleted:
        return False
    if owner_id:
        await context.storage.discard_prefix(f"{owner_id}/{chart_id}/")
    return True


async def purge_account(
    context: AppContext, sonolus_id: str, grace_days: int | None = None
) -> bool:
    """Delete an account's row, or empty a banned one past its grace period, then its files.

    Args:
        context: App context.
        sonolus_id: Account id.
        grace_days: Only purge an account marked for deletion at least this many days ago, None
            to delete it entirely and immediately.

    Returns:
        bool: Whether the account was purged.
    """
    async with context.database.transaction() as connection:
        banned = False
        if grace_days is not None:
            due_banned = await accounts_db.lock_due_account(connection, sonolus_id, grace_days)
            if due_banned is None:
                return False
            banned = due_banned
        replay_prefixes = await accounts_db.get_replay_prefixes(connection, sonolus_id)
        if banned:
            await accounts_db.tombstone_account(connection, sonolus_id)
            purged = True
        else:
            purged = await accounts_db.delete_account(connection, sonolus_id, grace_days)
    if not purged and grace_days is not None:
        return False
    await context.storage.discard_prefix(f"{sonolus_id}/")
    for prefix in replay_prefixes:
        await context.storage.discard_prefix(prefix)
    return purged
