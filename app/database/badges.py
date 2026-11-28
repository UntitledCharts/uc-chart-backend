from datetime import datetime

from pydantic import BaseModel

from app.database.core import Connection, fetch_all, fetch_value


class BadgeRow(BaseModel):
    """A badge held by an account.

    Attributes:
        sonolus_id: Account id.
        badge: Badge name.
        level: Badge level.
        granted_by: Account that granted it, if known.
        granted_at: Grant time.
    """

    sonolus_id: str
    badge: str
    level: int
    granted_by: str | None = None
    granted_at: datetime


async def get_badges(connection: Connection, sonolus_id: str) -> list[BadgeRow]:
    """Fetch an account's badges.

    Args:
        connection: Database connection.
        sonolus_id: Account id.

    Returns:
        list[BadgeRow]: The badges, oldest grant first.
    """
    return await fetch_all(
        connection,
        BadgeRow,
        """
        SELECT sonolus_id, badge, level, granted_by, granted_at FROM account_badges
        WHERE sonolus_id = $1 ORDER BY granted_at, badge
        """,
        sonolus_id,
    )


async def get_badges_for(
    connection: Connection, sonolus_ids: list[str]
) -> dict[str, list[BadgeRow]]:
    """Fetch the badges of several accounts.

    Args:
        connection: Database connection.
        sonolus_ids: Account ids.

    Returns:
        dict[str, list[BadgeRow]]: Badges keyed by account id, oldest grant first; accounts
            without any are left out.
    """
    if not sonolus_ids:
        return {}
    rows = await fetch_all(
        connection,
        BadgeRow,
        """
        SELECT sonolus_id, badge, level, granted_by, granted_at FROM account_badges
        WHERE sonolus_id = ANY($1::text[]) ORDER BY granted_at, badge
        """,
        list(set(sonolus_ids)),
    )
    grouped: dict[str, list[BadgeRow]] = {}
    for row in rows:
        grouped.setdefault(row.sonolus_id, []).append(row)
    return grouped


async def set_badge(
    connection: Connection,
    sonolus_id: str,
    badge: str,
    level: int,
    granted_by: str | None,
) -> int | None:
    """Grant a badge or change its level.

    Args:
        connection: Database connection inside a transaction.
        sonolus_id: Account id.
        badge: Badge name.
        level: New level.
        granted_by: Granting account, or None for the admin key.

    Returns:
        int | None: The previous level, or None when the account didn't hold the badge.
    """
    previous = await fetch_value(
        connection,
        "SELECT level FROM account_badges WHERE sonolus_id = $1 AND badge = $2 FOR UPDATE",
        sonolus_id,
        badge,
    )
    await connection.execute(
        """
        INSERT INTO account_badges (sonolus_id, badge, level, granted_by)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (sonolus_id, badge) DO UPDATE
        SET level = EXCLUDED.level, granted_by = EXCLUDED.granted_by, granted_at = CURRENT_TIMESTAMP
        """,
        sonolus_id,
        badge,
        level,
        granted_by,
    )
    return int(previous) if previous is not None else None


async def remove_badge(connection: Connection, sonolus_id: str, badge: str) -> int | None:
    """Take a badge away.

    Args:
        connection: Database connection.
        sonolus_id: Account id.
        badge: Badge name.

    Returns:
        int | None: The removed level, or None when the account didn't hold the badge.
    """
    level = await fetch_value(
        connection,
        "DELETE FROM account_badges WHERE sonolus_id = $1 AND badge = $2 RETURNING level",
        sonolus_id,
        badge,
    )
    return int(level) if level is not None else None
