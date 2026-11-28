from datetime import datetime

from pydantic import BaseModel

from app.database.core import Connection, fetch_all, fetch_one


class NotificationRow(BaseModel):
    """A notification sent to an account.

    Attributes:
        id: Notification id.
        user_id: Recipient account id.
        title: Title.
        content: Body text.
        is_read: Whether the recipient read it.
        created_at: Sending time.
    """

    id: int
    user_id: str
    title: str
    content: str | None = None
    is_read: bool = False
    created_at: datetime


COLUMNS = "id, user_id, title, content, COALESCE(is_read, false) AS is_read, created_at"


async def create_notification(
    connection: Connection, user_id: str, title: str, content: str | None
) -> NotificationRow | None:
    """Send a notification.

    Args:
        connection: Database connection.
        user_id: Recipient account id.
        title: Title.
        content: Body text.

    Returns:
        NotificationRow | None: The notification, or None when the recipient doesn't exist.
    """
    return await fetch_one(
        connection,
        NotificationRow,
        f"""
        INSERT INTO notifications (user_id, title, content)
        SELECT $1, $2, $3 WHERE EXISTS (SELECT 1 FROM accounts WHERE sonolus_id = $1)
        RETURNING {COLUMNS}
        """,
        user_id,
        title,
        content,
    )


async def list_notifications(
    connection: Connection, user_id: str, offset: int, limit: int, only_unread: bool
) -> tuple[list[NotificationRow], int]:
    """List an account's notifications, newest first.

    Args:
        connection: Database connection.
        user_id: Recipient account id.
        offset: Notifications skipped.
        limit: Most notifications returned.
        only_unread: Only unread notifications.

    Returns:
        tuple[list[NotificationRow], int]: The page and the total number of matches.
    """
    unread_filter = "AND COALESCE(is_read, false) = false" if only_unread else ""
    total = await connection.fetchval(
        f"SELECT COUNT(*) FROM notifications WHERE user_id = $1 {unread_filter}",
        user_id,
    )
    rows = await fetch_all(
        connection,
        NotificationRow,
        f"""
        SELECT {COLUMNS} FROM notifications
        WHERE user_id = $1 {unread_filter}
        ORDER BY created_at DESC, id DESC
        LIMIT $2 OFFSET $3
        """,
        user_id,
        limit,
        offset,
    )
    return rows, int(total or 0)


async def unread_count(connection: Connection, user_id: str) -> int:
    """Count an account's unread notifications.

    Args:
        connection: Database connection.
        user_id: Recipient account id.

    Returns:
        int: Unread notifications.
    """
    count = await connection.fetchval(
        "SELECT COUNT(*) FROM notifications WHERE user_id = $1 AND COALESCE(is_read, false) = false",
        user_id,
    )
    return int(count or 0)


async def read_notification(
    connection: Connection, notification_id: int, user_id: str
) -> NotificationRow | None:
    """Fetch a notification and mark it read.

    Args:
        connection: Database connection.
        notification_id: Notification id.
        user_id: Recipient account id.

    Returns:
        NotificationRow | None: The notification, or None when not found for the account.
    """
    return await fetch_one(
        connection,
        NotificationRow,
        f"""
        UPDATE notifications SET is_read = true
        WHERE id = $1 AND user_id = $2
        RETURNING {COLUMNS}
        """,
        notification_id,
        user_id,
    )


async def set_read(
    connection: Connection, notification_id: int, user_id: str, is_read: bool
) -> NotificationRow | None:
    """Mark a notification read or unread.

    Args:
        connection: Database connection.
        notification_id: Notification id.
        user_id: Recipient account id.
        is_read: New read state.

    Returns:
        NotificationRow | None: The notification, or None when not found for the account.
    """
    return await fetch_one(
        connection,
        NotificationRow,
        f"""
        UPDATE notifications SET is_read = $3
        WHERE id = $1 AND user_id = $2
        RETURNING {COLUMNS}
        """,
        notification_id,
        user_id,
        is_read,
    )


async def delete_notification(connection: Connection, notification_id: int, user_id: str) -> bool:
    """Delete a notification.

    Args:
        connection: Database connection.
        notification_id: Notification id.
        user_id: Recipient account id.

    Returns:
        bool: Whether a notification was deleted.
    """
    deleted = await connection.fetchval(
        "DELETE FROM notifications WHERE id = $1 AND user_id = $2 RETURNING true",
        notification_id,
        user_id,
    )
    return bool(deleted)
