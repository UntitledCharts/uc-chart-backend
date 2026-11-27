from datetime import datetime

from pydantic import BaseModel

from app.database.core import Connection, fetch_all, fetch_one

ONGOING_SQL = """
    p.cancelled = false
    AND p.ended_at IS NULL
    AND ((p.target_type = 'VIEW' AND p.view_count < p.target_amount)
         OR (p.target_type = 'CLICK' AND p.click_count < p.target_amount))
"""


class ActivePromotionRow(BaseModel):
    """A promotion that can currently be shown.

    Attributes:
        id: Promotion id.
        chart_id: Promoted chart id.
        chart_rating: Rounded rating of the promoted chart.
    """

    id: int
    chart_id: str
    chart_rating: int


class PromotionRow(BaseModel):
    """A promotion campaign.

    Attributes:
        id: Promotion id.
        chart_id: Promoted chart id.
        target_type: Whether the campaign ends after views or clicks.
        target_amount: Views or clicks until the campaign ends.
        view_count: Views counted.
        click_count: Clicks counted.
        logged_in_click_count: Clicks by logged-in viewers.
        cancelled: Whether the campaign was cancelled.
        created_at: Creation time.
        ended_at: End time.
    """

    id: int
    chart_id: str
    target_type: str
    target_amount: int
    view_count: int
    click_count: int
    logged_in_click_count: int
    cancelled: bool
    created_at: datetime | None = None
    ended_at: datetime | None = None


async def active_promotions(connection: Connection) -> list[ActivePromotionRow]:
    """List promotions that can be shown now.

    Args:
        connection: Database connection.

    Returns:
        list[ActivePromotionRow]: The promotions.
    """
    return await fetch_all(
        connection,
        ActivePromotionRow,
        f"""
        SELECT p.id, p.chart_id, ROUND(c.rating)::int AS chart_rating
        FROM promotions p
        JOIN charts c ON c.id = p.chart_id
        JOIN accounts a ON a.sonolus_id = c.author
        WHERE {ONGOING_SQL}
          AND c.status = 'PUBLIC' AND c.deleted_at IS NULL AND a.deleted_at IS NULL
          AND COALESCE(a.banned, false) = false
        """,
    )


async def shown_counts(
    connection: Connection, sonolus_id: str, promotion_ids: list[int]
) -> dict[int, int]:
    """How often each promotion was shown to an account.

    Args:
        connection: Database connection.
        sonolus_id: Account id.
        promotion_ids: Promotion ids.

    Returns:
        dict[int, int]: Times shown keyed by promotion id.
    """
    records = await connection.fetch(
        """
        SELECT promotion_id, shown_count FROM promotion_user_views
        WHERE sonolus_id = $1 AND promotion_id = ANY($2::int[])
        """,
        sonolus_id,
        promotion_ids,
    )
    return {record["promotion_id"]: record["shown_count"] for record in records}


async def existing_view_code(
    connection: Connection, promotion_id: int, viewer_key: str, window_hours: int
) -> str | None:
    """Find the view code a viewer already got within the dedup window.

    Args:
        connection: Database connection.
        promotion_id: Promotion id.
        viewer_key: Hashed viewer identity.
        window_hours: Dedup window.

    Returns:
        str | None: The view code, or None when the viewer counts as new.
    """
    return await connection.fetchval(
        """
        SELECT view_code FROM promotion_viewers
        WHERE promotion_id = $1 AND viewer_key = $2
          AND first_seen_at > CURRENT_TIMESTAMP - make_interval(hours => $3)
        """,
        promotion_id,
        viewer_key,
        window_hours,
    )


async def claim_view(
    connection: Connection,
    promotion_id: int,
    viewer_key: str,
    view_code: str,
    window_hours: int,
) -> bool:
    """Record a new viewer and their view code, counting the view.

    Args:
        connection: Database connection inside a transaction.
        promotion_id: Promotion id.
        viewer_key: Hashed viewer identity.
        view_code: Unique view code for click attribution.
        window_hours: Dedup window.

    Returns:
        bool: Whether the view was counted; False when the view code collides.
    """
    claimed = await connection.fetchval(
        """
        INSERT INTO promotion_viewers (promotion_id, viewer_key, view_code, first_seen_at)
        VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
        ON CONFLICT (promotion_id, viewer_key) DO UPDATE
        SET view_code = EXCLUDED.view_code, first_seen_at = EXCLUDED.first_seen_at
        WHERE promotion_viewers.first_seen_at <= CURRENT_TIMESTAMP - make_interval(hours => $4)
        RETURNING true
        """,
        promotion_id,
        viewer_key,
        view_code,
        window_hours,
    )
    if not claimed:
        return False
    inserted = await connection.fetchval(
        """
        INSERT INTO promotion_views (promotion_id, view_code) VALUES ($1, $2)
        ON CONFLICT DO NOTHING RETURNING true
        """,
        promotion_id,
        view_code,
    )
    if not inserted:
        raise ViewCodeCollision
    await connection.execute(
        """
        UPDATE promotions
        SET view_count = view_count + 1,
            ended_at = CASE
                WHEN target_type = 'VIEW' AND view_count + 1 >= target_amount AND ended_at IS NULL
                THEN CURRENT_TIMESTAMP ELSE ended_at END
        WHERE id = $1
        """,
        promotion_id,
    )
    return True


class ViewCodeCollision(Exception):
    """Raised when a generated view code is already in use."""


async def increment_user_shown(connection: Connection, promotion_id: int, sonolus_id: str) -> None:
    """Count a promotion shown to an account for frequency capping.

    Args:
        connection: Database connection.
        promotion_id: Promotion id.
        sonolus_id: Account id.
    """
    await connection.execute(
        """
        INSERT INTO promotion_user_views (promotion_id, sonolus_id, shown_count) VALUES ($1, $2, 1)
        ON CONFLICT (promotion_id, sonolus_id) DO UPDATE SET shown_count = promotion_user_views.shown_count + 1
        """,
        promotion_id,
        sonolus_id,
    )


async def resolve_click(
    connection: Connection,
    chart_id: str,
    view_code: str,
    logged_in: bool,
    window_hours: int,
) -> bool:
    """Count a click on a served promotion, once per view code.

    Args:
        connection: Database connection.
        chart_id: Promoted chart id.
        view_code: View code from the serve response.
        logged_in: Whether the clicking viewer is logged in.
        window_hours: How long view codes stay valid.

    Returns:
        bool: Whether the click was counted.
    """
    counted = await connection.fetchval(
        """
        WITH claimed AS (
            DELETE FROM promotion_views pv
            USING promotions p
            WHERE pv.promotion_id = p.id AND p.chart_id = $1 AND pv.view_code = $2
              AND pv.created_at > CURRENT_TIMESTAMP - make_interval(hours => $4)
            RETURNING pv.promotion_id
        ),
        bumped AS (
            UPDATE promotions
            SET click_count = click_count + 1,
                logged_in_click_count = logged_in_click_count + CASE WHEN $3::bool THEN 1 ELSE 0 END,
                ended_at = CASE
                    WHEN target_type = 'CLICK' AND click_count + 1 >= target_amount AND ended_at IS NULL
                    THEN CURRENT_TIMESTAMP ELSE ended_at END
            WHERE id IN (SELECT promotion_id FROM claimed)
            RETURNING id
        )
        SELECT COUNT(*) > 0 FROM bumped
        """,
        chart_id,
        view_code,
        logged_in,
        window_hours,
    )
    return bool(counted)


async def create_promotion(
    connection: Connection, chart_id: str, target_type: str, target_amount: int
) -> int:
    """Start a promotion campaign.

    Args:
        connection: Database connection.
        chart_id: Promoted chart id.
        target_type: ``VIEW`` or ``CLICK``.
        target_amount: Views or clicks until the campaign ends.

    Returns:
        int: Promotion id.
    """
    return await connection.fetchval(
        "INSERT INTO promotions (chart_id, target_type, target_amount) VALUES ($1, $2, $3) RETURNING id",
        chart_id,
        target_type,
        target_amount,
    )


async def get_promotion(connection: Connection, promotion_id: int) -> PromotionRow | None:
    """Fetch a promotion campaign.

    Args:
        connection: Database connection.
        promotion_id: Promotion id.

    Returns:
        PromotionRow | None: The campaign, or None.
    """
    return await fetch_one(
        connection, PromotionRow, "SELECT * FROM promotions WHERE id = $1", promotion_id
    )


async def list_promotions(connection: Connection) -> list[PromotionRow]:
    """List every promotion campaign, newest first.

    Args:
        connection: Database connection.

    Returns:
        list[PromotionRow]: The campaigns.
    """
    return await fetch_all(
        connection, PromotionRow, "SELECT * FROM promotions ORDER BY created_at DESC"
    )


async def cancel_promotion(connection: Connection, promotion_id: int) -> bool:
    """Cancel a promotion campaign.

    Args:
        connection: Database connection.
        promotion_id: Promotion id.

    Returns:
        bool: Whether a running campaign was cancelled.
    """
    cancelled = await connection.fetchval(
        """
        UPDATE promotions SET cancelled = true, ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)
        WHERE id = $1 AND cancelled = false RETURNING true
        """,
        promotion_id,
    )
    return bool(cancelled)


async def expire_stale_rows(connection: Connection, window_hours: int, limit: int) -> int:
    """Delete a batch of expired view codes, viewer records and inactive frequency counters.

    Args:
        connection: Database connection.
        window_hours: View code and dedup window.
        limit: Most rows deleted per table.

    Returns:
        int: Number of rows deleted.
    """
    total = 0
    for statement in (
        """
        DELETE FROM promotion_views WHERE (promotion_id, view_code) IN (
            SELECT promotion_id, view_code FROM promotion_views
            WHERE created_at <= CURRENT_TIMESTAMP - make_interval(hours => $1) LIMIT $2
        )
        """,
        """
        DELETE FROM promotion_viewers WHERE (promotion_id, viewer_key) IN (
            SELECT promotion_id, viewer_key FROM promotion_viewers
            WHERE first_seen_at <= CURRENT_TIMESTAMP - make_interval(hours => $1) LIMIT $2
        )
        """,
    ):
        result = await connection.execute(statement, window_hours, limit)
        total += int(result.split()[-1])
    result = await connection.execute(
        """
        DELETE FROM promotion_user_views WHERE (promotion_id, sonolus_id) IN (
            SELECT puv.promotion_id, puv.sonolus_id FROM promotion_user_views puv
            JOIN promotions p ON p.id = puv.promotion_id
            WHERE p.cancelled OR p.ended_at IS NOT NULL
            LIMIT $1
        )
        """,
        limit,
    )
    return total + int(result.split()[-1])
