from datetime import datetime

from pydantic import BaseModel

from app.database.core import Connection, fetch_all, fetch_one


class CommentRow(BaseModel):
    """A comment on a chart.

    Attributes:
        id: Comment id.
        commenter: Commenting account id.
        commenter_username: Commenter's display name.
        commenter_handle: Commenter's handle.
        content: Comment text.
        created_at: Posting time.
        deleted_at: Deletion time, if deleted.
        chart_id: Chart id.
    """

    id: int
    commenter: str | None = None
    commenter_username: str | None = None
    commenter_handle: int | None = None
    content: str
    created_at: datetime
    deleted_at: datetime | None = None
    chart_id: str | None = None


COMMENT_COLUMNS = """
    cm.id, cm.commenter, a.sonolus_username AS commenter_username, a.sonolus_handle AS commenter_handle,
    cm.content, cm.created_at, cm.deleted_at, cm.chart_id
"""


async def create_comment(
    connection: Connection, chart_id: str, commenter: str, content: str
) -> CommentRow | None:
    """Post a comment.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        commenter: Commenting account id.
        content: Comment text.

    Returns:
        CommentRow | None: The comment, or None when the chart doesn't exist.
    """
    return await fetch_one(
        connection,
        CommentRow,
        f"""
        WITH inserted AS (
            INSERT INTO comments (commenter, content, created_at, chart_id)
            SELECT $2, $3, CURRENT_TIMESTAMP, $1
            WHERE EXISTS (SELECT 1 FROM charts WHERE id = $1)
            RETURNING *
        )
        SELECT {COMMENT_COLUMNS} FROM inserted cm LEFT JOIN accounts a ON a.sonolus_id = cm.commenter
        """,
        chart_id,
        commenter,
        content,
    )


async def get_comment(connection: Connection, chart_id: str, comment_id: int) -> CommentRow | None:
    """Fetch a comment of a chart.

    Args:
        connection: Database connection.
        chart_id: Chart id the comment must belong to.
        comment_id: Comment id.

    Returns:
        CommentRow | None: The comment, or None.
    """
    return await fetch_one(
        connection,
        CommentRow,
        f"""
        SELECT {COMMENT_COLUMNS} FROM comments cm LEFT JOIN accounts a ON a.sonolus_id = cm.commenter
        WHERE cm.id = $1 AND cm.chart_id = $2
        """,
        comment_id,
        chart_id,
    )


async def soft_delete_comment(connection: Connection, comment_id: int) -> bool:
    """Mark a comment deleted.

    Args:
        connection: Database connection.
        comment_id: Comment id.

    Returns:
        bool: Whether a live comment was deleted.
    """
    deleted = await connection.fetchval(
        """
        UPDATE comments SET deleted_at = CURRENT_TIMESTAMP
        WHERE id = $1 AND deleted_at IS NULL
        RETURNING true
        """,
        comment_id,
    )
    return bool(deleted)


async def list_comments(
    connection: Connection, chart_id: str, page: int, page_size: int
) -> tuple[list[CommentRow], int]:
    """List a chart's comments, newest first, including deleted ones.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        page: Zero-based page.
        page_size: Comments per page.

    Returns:
        tuple[list[CommentRow], int]: The page and the total number of comments.
    """
    total = await connection.fetchval("SELECT COUNT(*) FROM comments WHERE chart_id = $1", chart_id)
    rows = await fetch_all(
        connection,
        CommentRow,
        f"""
        SELECT {COMMENT_COLUMNS} FROM comments cm LEFT JOIN accounts a ON a.sonolus_id = cm.commenter
        WHERE cm.chart_id = $1
        ORDER BY cm.created_at DESC, cm.id DESC
        LIMIT $2 OFFSET $3
        """,
        chart_id,
        page_size,
        page * page_size,
    )
    return rows, int(total)
