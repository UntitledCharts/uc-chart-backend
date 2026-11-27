from datetime import datetime

from pydantic import BaseModel

from app.database.core import Connection, fetch_all, fetch_one, fetch_value

ACCOUNT_COLUMNS = """
    a.sonolus_id,
    a.sonolus_handle,
    a.sonolus_username,
    a.chart_upload_cooldown,
    a.created_at,
    a.updated_at,
    COALESCE(a.banned, false) AS banned,
    a.description,
    a.profile_hash,
    a.banner_hash,
    a.deleted_at,
    a.ad_views,
    a.ad_clicks,
    a.recent_opened_levels
"""


class AccountRow(BaseModel):
    """An account row.

    Attributes:
        sonolus_id: Sonolus account id.
        sonolus_handle: Sonolus handle number.
        sonolus_username: Sonolus display name.
        chart_upload_cooldown: Time before which the account can't upload again.
        created_at: Creation time.
        updated_at: Last update time.
        banned: Whether the account is banned.
        description: Profile description.
        profile_hash: Hash of the avatar image.
        banner_hash: Hash of the banner image.
        deleted_at: Time the account was marked for deletion.
        ad_views: Promotions shown to the account.
        ad_clicks: Promotions the account opened.
        recent_opened_levels: Ratings of recently opened charts.
    """

    sonolus_id: str
    sonolus_handle: int
    sonolus_username: str
    chart_upload_cooldown: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    banned: bool = False
    description: str | None = None
    profile_hash: str | None = None
    banner_hash: str | None = None
    deleted_at: datetime | None = None
    ad_views: int = 0
    ad_clicks: int = 0
    recent_opened_levels: list[int] = []


class AccountStatsRow(BaseModel):
    """Aggregated statistics of an account.

    Attributes:
        sonolus_id: Sonolus account id.
        sonolus_handle: Sonolus handle number.
        liked_charts_count: Charts the account liked.
        comments_count: Comments the account wrote.
        charts_published: Public charts the account owns.
        likes_received: Likes on the account's charts.
        comments_received: Comments on the account's charts.
    """

    sonolus_id: str
    sonolus_handle: int
    liked_charts_count: int
    comments_count: int
    charts_published: int
    likes_received: int
    comments_received: int


async def get_account(connection: Connection, sonolus_id: str) -> AccountRow | None:
    """Fetch an account by id.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.

    Returns:
        AccountRow | None: The account, or None.
    """
    return await fetch_one(
        connection,
        AccountRow,
        f"SELECT {ACCOUNT_COLUMNS} FROM accounts a WHERE a.sonolus_id = $1",
        sonolus_id,
    )


async def get_account_by_handle(connection: Connection, handle: int) -> AccountRow | None:
    """Fetch an account by Sonolus handle.

    Args:
        connection: Database connection.
        handle: Sonolus handle number.

    Returns:
        AccountRow | None: The account, or None.
    """
    return await fetch_one(
        connection,
        AccountRow,
        f"SELECT {ACCOUNT_COLUMNS} FROM accounts a WHERE a.sonolus_handle = $1 "
        "ORDER BY a.deleted_at NULLS FIRST, a.created_at LIMIT 1",
        handle,
    )


async def get_accounts(connection: Connection, sonolus_ids: list[str]) -> dict[str, AccountRow]:
    """Fetch several accounts by id.

    Args:
        connection: Database connection.
        sonolus_ids: Sonolus account ids.

    Returns:
        dict[str, AccountRow]: Accounts keyed by id.
    """
    if not sonolus_ids:
        return {}
    rows = await fetch_all(
        connection,
        AccountRow,
        f"SELECT {ACCOUNT_COLUMNS} FROM accounts a WHERE a.sonolus_id = ANY($1::text[])",
        list(set(sonolus_ids)),
    )
    return {row.sonolus_id: row for row in rows}


async def upsert_account(
    connection: Connection, sonolus_id: str, sonolus_handle: int, sonolus_username: str
) -> AccountRow:
    """Create an account or refresh its display name.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
        sonolus_handle: Sonolus handle number.
        sonolus_username: Sonolus display name.

    Returns:
        AccountRow: The account.
    """
    row = await fetch_one(
        connection,
        AccountRow,
        f"""
        WITH upserted AS (
            INSERT INTO accounts (sonolus_id, sonolus_handle, sonolus_username)
            VALUES ($1, $2, $3)
            ON CONFLICT (sonolus_id) DO UPDATE SET sonolus_username = EXCLUDED.sonolus_username
            RETURNING *
        )
        SELECT {ACCOUNT_COLUMNS} FROM upserted a
        """,
        sonolus_id,
        sonolus_handle,
        sonolus_username,
    )
    assert row is not None
    return row


async def set_banned(connection: Connection, sonolus_id: str, banned: bool) -> bool:
    """Ban or unban an account.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
        banned: New banned state.

    Returns:
        bool: The previous banned state.
    """
    previous = await fetch_value(
        connection,
        """
        UPDATE accounts a SET banned = $2, updated_at = CURRENT_TIMESTAMP
        FROM (SELECT sonolus_id, COALESCE(banned, false) AS banned FROM accounts WHERE sonolus_id = $1 FOR UPDATE) previous
        WHERE a.sonolus_id = previous.sonolus_id
        RETURNING previous.banned
        """,
        sonolus_id,
        banned,
    )
    return bool(previous)


async def set_pending_deletion(
    connection: Connection, sonolus_id: str, pending: bool
) -> datetime | None:
    """Mark an account for deletion or clear the mark.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
        pending: Whether the account should be pending deletion.

    Returns:
        datetime | None: The previous deletion mark.
    """
    return await fetch_value(
        connection,
        """
        UPDATE accounts a
        SET deleted_at = CASE WHEN $2 THEN COALESCE(a.deleted_at, CURRENT_TIMESTAMP) ELSE NULL END
        FROM (SELECT sonolus_id, deleted_at FROM accounts WHERE sonolus_id = $1 FOR UPDATE) previous
        WHERE a.sonolus_id = previous.sonolus_id
        RETURNING previous.deleted_at
        """,
        sonolus_id,
        pending,
    )


async def update_description(
    connection: Connection, sonolus_id: str, description: str | None
) -> None:
    """Set an account's profile description.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
        description: New description, or None to clear it.
    """
    await connection.execute(
        "UPDATE accounts SET description = $2, updated_at = CURRENT_TIMESTAMP WHERE sonolus_id = $1",
        sonolus_id,
        description,
    )


async def update_profile_hash(
    connection: Connection, sonolus_id: str, file_hash: str | None
) -> None:
    """Set an account's avatar hash.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
        file_hash: New avatar hash, or None to remove it.
    """
    await connection.execute(
        "UPDATE accounts SET profile_hash = $2, updated_at = CURRENT_TIMESTAMP WHERE sonolus_id = $1",
        sonolus_id,
        file_hash,
    )


async def update_banner_hash(
    connection: Connection, sonolus_id: str, file_hash: str | None
) -> None:
    """Set an account's banner hash.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
        file_hash: New banner hash, or None to remove it.
    """
    await connection.execute(
        "UPDATE accounts SET banner_hash = $2, updated_at = CURRENT_TIMESTAMP WHERE sonolus_id = $1",
        sonolus_id,
        file_hash,
    )


async def reserve_upload_cooldown(
    connection: Connection, sonolus_id: str, cooldown_seconds: int
) -> datetime | None:
    """Atomically start an upload cooldown if none is running.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
        cooldown_seconds: Cooldown length.

    Returns:
        datetime | None: None when reserved, otherwise the time the running cooldown ends.
    """
    reserved = await fetch_value(
        connection,
        """
        UPDATE accounts
        SET chart_upload_cooldown = CURRENT_TIMESTAMP + make_interval(secs => $2)
        WHERE sonolus_id = $1
          AND (chart_upload_cooldown IS NULL OR chart_upload_cooldown <= CURRENT_TIMESTAMP)
        RETURNING true
        """,
        sonolus_id,
        cooldown_seconds,
    )
    if reserved:
        return None
    return await fetch_value(
        connection,
        "SELECT chart_upload_cooldown FROM accounts WHERE sonolus_id = $1",
        sonolus_id,
    )


async def release_upload_cooldown(connection: Connection, sonolus_id: str) -> None:
    """Cancel an upload cooldown after a failed upload.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
    """
    await connection.execute(
        "UPDATE accounts SET chart_upload_cooldown = NULL WHERE sonolus_id = $1",
        sonolus_id,
    )


async def record_opened_level(
    connection: Connection, sonolus_id: str, rating: int, keep: int
) -> None:
    """Remember the rating of a chart the account opened.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
        rating: Rounded chart rating.
        keep: Number of ratings kept.
    """
    await connection.execute(
        """
        UPDATE accounts
        SET recent_opened_levels = (array_append(recent_opened_levels, $2))[
            GREATEST(1, cardinality(recent_opened_levels) + 2 - $3):
        ]
        WHERE sonolus_id = $1
        """,
        sonolus_id,
        rating,
        keep,
    )


async def record_ad_view(connection: Connection, sonolus_id: str) -> None:
    """Count a promotion shown to an account.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
    """
    await connection.execute(
        "UPDATE accounts SET ad_views = ad_views + 1 WHERE sonolus_id = $1", sonolus_id
    )


async def record_ad_click(connection: Connection, sonolus_id: str) -> None:
    """Count a promotion opened by an account.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
    """
    await connection.execute(
        "UPDATE accounts SET ad_clicks = ad_clicks + 1 WHERE sonolus_id = $1",
        sonolus_id,
    )


async def get_stats(connection: Connection, sonolus_id: str) -> AccountStatsRow | None:
    """Aggregate an account's activity.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.

    Returns:
        AccountStatsRow | None: The statistics, or None when the account doesn't exist.
    """
    return await fetch_one(
        connection,
        AccountStatsRow,
        """
        SELECT
            a.sonolus_id,
            a.sonolus_handle,
            (SELECT COUNT(*) FROM chart_likes cl WHERE cl.sonolus_id = a.sonolus_id) AS liked_charts_count,
            (SELECT COUNT(*) FROM comments cm WHERE cm.commenter = a.sonolus_id) AS comments_count,
            (SELECT COUNT(*) FROM charts c
                WHERE c.author = a.sonolus_id AND c.status = 'PUBLIC' AND c.deleted_at IS NULL) AS charts_published,
            (SELECT COUNT(*) FROM chart_likes cl JOIN charts c ON c.id = cl.chart_id
                WHERE c.author = a.sonolus_id AND c.deleted_at IS NULL) AS likes_received,
            (SELECT COUNT(*) FROM comments cm JOIN charts c ON c.id = cm.chart_id
                WHERE c.author = a.sonolus_id AND c.deleted_at IS NULL) AS comments_received
        FROM accounts a
        WHERE a.sonolus_id = $1
        """,
        sonolus_id,
    )


DUE_FOR_PURGE = (
    "deleted_at IS NOT NULL AND deleted_at <= CURRENT_TIMESTAMP - make_interval(days => $2)"
)

TOMBSTONED = """
    COALESCE(a.banned, false)
    AND a.profile_hash IS NULL AND a.banner_hash IS NULL AND a.description IS NULL
    AND NOT EXISTS (SELECT 1 FROM charts c WHERE c.author = a.sonolus_id)
    AND NOT EXISTS (SELECT 1 FROM comments cm WHERE cm.commenter = a.sonolus_id)
    AND NOT EXISTS (SELECT 1 FROM chart_likes cl WHERE cl.sonolus_id = a.sonolus_id)
    AND NOT EXISTS (SELECT 1 FROM leaderboards l WHERE l.submitter = a.sonolus_id)
    AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.user_id = a.sonolus_id)
    AND NOT EXISTS (SELECT 1 FROM account_connections ac WHERE ac.sonolus_id = a.sonolus_id)
    AND NOT EXISTS (SELECT 1 FROM account_permissions ap WHERE ap.sonolus_id = a.sonolus_id)
    AND NOT EXISTS (SELECT 1 FROM chart_collaborators cc WHERE cc.sonolus_id = a.sonolus_id)
    AND NOT EXISTS (SELECT 1 FROM chart_collaborator_invites ci WHERE ci.sonolus_id = a.sonolus_id)
"""


async def list_accounts_to_purge(connection: Connection, grace_days: int, limit: int) -> list[str]:
    """List accounts whose deletion grace period is over, skipping banned ones already emptied.

    Args:
        connection: Database connection.
        grace_days: Grace period in days.
        limit: Most accounts returned.

    Returns:
        list[str]: Sonolus account ids.
    """
    records = await connection.fetch(
        f"""
        SELECT a.sonolus_id FROM accounts a
        WHERE a.deleted_at IS NOT NULL
          AND a.deleted_at <= CURRENT_TIMESTAMP - make_interval(days => $1)
          AND NOT ({TOMBSTONED})
        ORDER BY a.deleted_at
        LIMIT $2
        """,
        grace_days,
        limit,
    )
    return [record["sonolus_id"] for record in records]


async def lock_due_account(connection: Connection, sonolus_id: str, grace_days: int) -> bool | None:
    """Lock an account whose deletion grace period is over.

    Args:
        connection: Database connection inside a transaction.
        sonolus_id: Sonolus account id.
        grace_days: Grace period in days.

    Returns:
        bool | None: Whether the account is banned, or None when it isn't due for purging.
    """
    return await fetch_value(
        connection,
        f"""
        SELECT COALESCE(banned, false) FROM accounts
        WHERE sonolus_id = $1 AND {DUE_FOR_PURGE}
        FOR UPDATE
        """,
        sonolus_id,
        grace_days,
    )


async def delete_account(
    connection: Connection, sonolus_id: str, grace_days: int | None = None
) -> bool:
    """Delete an account row and everything that cascades from it.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.
        grace_days: Only delete an account marked for deletion at least this many days ago,
            None to delete it regardless.

    Returns:
        bool: Whether a row was deleted.
    """
    if grace_days is None:
        deleted = await fetch_value(
            connection,
            "DELETE FROM accounts WHERE sonolus_id = $1 RETURNING true",
            sonolus_id,
        )
    else:
        deleted = await fetch_value(
            connection,
            f"DELETE FROM accounts WHERE sonolus_id = $1 AND {DUE_FOR_PURGE} RETURNING true",
            sonolus_id,
            grace_days,
        )
    return bool(deleted)


async def tombstone_account(connection: Connection, sonolus_id: str) -> None:
    """Delete everything a banned account made but keep its row, so the ban survives a new login.

    Args:
        connection: Database connection inside a transaction.
        sonolus_id: Sonolus account id.
    """
    for statement in (
        "DELETE FROM charts WHERE author = $1",
        "DELETE FROM comments WHERE commenter = $1",
        "DELETE FROM chart_likes WHERE sonolus_id = $1",
        "DELETE FROM leaderboards WHERE submitter = $1",
        "DELETE FROM notifications WHERE user_id = $1",
        "DELETE FROM sessions WHERE sonolus_id = $1",
        "DELETE FROM oauth_tokens WHERE user_id = $1",
        "DELETE FROM oauth_authorization_codes WHERE user_id = $1",
        "DELETE FROM account_connections WHERE sonolus_id = $1",
        "DELETE FROM account_permissions WHERE sonolus_id = $1",
        "DELETE FROM chart_collaborators WHERE sonolus_id = $1",
        "DELETE FROM chart_collaborator_invites WHERE sonolus_id = $1",
        "DELETE FROM promotion_user_views WHERE sonolus_id = $1",
        """
        UPDATE accounts
        SET banned = true, profile_hash = NULL, banner_hash = NULL, description = NULL,
            recent_opened_levels = '{}', chart_upload_cooldown = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE sonolus_id = $1
        """,
    ):
        await connection.execute(statement, sonolus_id)


async def get_replay_prefixes(connection: Connection, sonolus_id: str) -> list[str]:
    """List storage prefixes of the account's replays on other accounts' charts.

    Args:
        connection: Database connection.
        sonolus_id: Sonolus account id.

    Returns:
        list[str]: Prefixes like ``{chart owner}/{chart id}/replays/{account}/``.
    """
    records = await connection.fetch(
        """
        SELECT DISTINCT c.author || '/' || c.id || '/replays/' || l.submitter || '/' AS prefix
        FROM leaderboards l
        JOIN charts c ON c.id = l.chart_id
        WHERE l.submitter = $1 AND c.author <> $1
        """,
        sonolus_id,
    )
    return [record["prefix"] for record in records]
