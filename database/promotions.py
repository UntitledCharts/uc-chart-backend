from database.query import ExecutableQuery, SelectQuery
from helpers.models import DBID, Count, Promotion, ActivePromotion


# a campaign is ongoing while the metric it targets is still under its goal
_ONGOING_SQL = (
    "(p.target_type = 'VIEW' AND p.view_count < p.target_amount)"
    " OR (p.target_type = 'CLICK' AND p.click_count < p.target_amount)"
)


def create_promotion(
    chart_id: str, target_type: str, target_amount: int
) -> SelectQuery[DBID]:
    return SelectQuery(
        DBID,
        """
            INSERT INTO promotions (chart_id, target_type, target_amount)
            VALUES ($1, $2, $3)
            RETURNING id::text AS id;
        """,
        chart_id,
        target_type,
        target_amount,
    )


def get_active_promotions() -> SelectQuery[ActivePromotion]:
    # ongoing campaigns whose chart is still public; rating is read live from the
    # chart so it tracks any rerates
    return SelectQuery(
        ActivePromotion,
        f"""
            SELECT p.id, p.chart_id, ROUND(c.rating)::int AS chart_rating
            FROM promotions p
            JOIN charts c ON p.chart_id = c.id
            WHERE p.cancelled = FALSE
                AND ({_ONGOING_SQL})
                AND c.status = 'PUBLIC'
                AND c.deleted_at IS NULL;
        """,
    )


def insert_view(promotion_id: int, view_code: str) -> ExecutableQuery:
    return ExecutableQuery(
        """
            INSERT INTO promotion_views (promotion_id, view_code)
            VALUES ($1, $2);
        """,
        promotion_id,
        view_code,
    )


def increment_view_count(promotion_id: int) -> ExecutableQuery:
    # a view-targeted campaign completes when it reaches its goal; stamp ended_at once
    return ExecutableQuery(
        """
            UPDATE promotions
            SET view_count = view_count + 1,
                ended_at = CASE
                    WHEN target_type = 'VIEW'
                        AND view_count + 1 >= target_amount
                        AND ended_at IS NULL
                    THEN CURRENT_TIMESTAMP
                    ELSE ended_at
                END
            WHERE id = $1;
        """,
        promotion_id,
    )


def cancel_for_chart(chart_id: str) -> ExecutableQuery:
    # a chart leaving PUBLIC can't gain views anymore, so cancel its ongoing
    # campaigns and drop their pending view codes immediately
    return ExecutableQuery(
        """
            WITH cancelled AS (
                UPDATE promotions
                SET cancelled = TRUE,
                    ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)
                WHERE chart_id = $1
                    AND cancelled = FALSE
                    AND (
                        (target_type = 'VIEW' AND view_count < target_amount)
                        OR (target_type = 'CLICK' AND click_count < target_amount)
                    )
                RETURNING id
            )
            DELETE FROM promotion_views
            WHERE promotion_id IN (SELECT id FROM cancelled);
        """,
        chart_id,
    )


def resolve_click(
    chart_id: str, view_code: str, max_age_hours: int = 24
) -> SelectQuery[Count]:
    # Deleting the view is the claim: it atomically counts at most one click and
    # frees the code. Expired or unknown codes match nothing (count 0).
    return SelectQuery(
        Count,
        f"""
            WITH claimed AS (
                DELETE FROM promotion_views pv
                USING promotions p
                WHERE pv.promotion_id = p.id
                    AND p.chart_id = $1
                    AND pv.view_code = $2
                    AND pv.created_at > CURRENT_TIMESTAMP - INTERVAL '{int(max_age_hours)} hours'
                RETURNING pv.promotion_id
            ),
            bumped AS (
                UPDATE promotions
                SET click_count = click_count + 1,
                    ended_at = CASE
                        WHEN target_type = 'CLICK'
                            AND click_count + 1 >= target_amount
                            AND ended_at IS NULL
                        THEN CURRENT_TIMESTAMP
                        ELSE ended_at
                    END
                WHERE id IN (SELECT promotion_id FROM claimed)
                RETURNING id
            )
            SELECT COUNT(*) AS total_count FROM bumped;
        """,
        chart_id,
        view_code,
    )


def expire_views(max_age_hours: int = 24) -> ExecutableQuery:
    return ExecutableQuery(
        f"""
            DELETE FROM promotion_views
            WHERE created_at <= CURRENT_TIMESTAMP - INTERVAL '{int(max_age_hours)} hours';
        """,
    )


def get_promotion(promotion_id: int) -> SelectQuery[Promotion]:
    return SelectQuery(
        Promotion,
        """
            SELECT
                p.id,
                p.chart_id,
                ROUND(c.rating)::int AS chart_rating,
                p.target_type,
                p.target_amount,
                p.view_count,
                p.click_count,
                CASE
                    WHEN p.cancelled THEN 'Cancelled'
                    WHEN (p.target_type = 'VIEW' AND p.view_count >= p.target_amount)
                        OR (p.target_type = 'CLICK' AND p.click_count >= p.target_amount)
                    THEN 'Complete'
                    ELSE 'Ongoing'
                END AS status
            FROM promotions p
            LEFT JOIN charts c ON p.chart_id = c.id
            WHERE p.id = $1;
        """,
        promotion_id,
    )
