from typing import List, Optional, Literal, Union
from decimal import Decimal

from database.query import ExecutableQuery, SelectQuery
from helpers.models import (
    Chart,
    ChartDBResponse,
    ChartByID,
    Count,
    DBID,
    ChartByIDLiked,
    ChartDBResponseLiked,
    ChartLikeTrend,
)


def create_chart(chart: Chart) -> SelectQuery[DBID]:
    tags_str = chart.tags if chart.tags else []

    return SelectQuery(
        DBID,
        """
            INSERT INTO charts (id, author, rating, description, chart_author, title, artists, tags, jacket_file_hash, music_file_hash, chart_file_hash, preview_file_hash, background_file_hash, background_v1_file_hash, background_v3_file_hash, status, created_at, updated_at)
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, 'PRIVATE', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            RETURNING id;
        """,
        chart.id,
        chart.author,
        chart.rating,
        chart.description,
        chart.chart_author,
        chart.title,
        chart.artists,
        tags_str,
        chart.jacket_file_hash,
        chart.music_file_hash,
        chart.chart_file_hash,
        chart.preview_file_hash if chart.preview_file_hash else None,
        chart.background_file_hash if chart.background_file_hash else None,
        chart.background_v1_file_hash,
        chart.background_v3_file_hash,
    )


def get_chart_list(
    page: int,
    items_per_page: int,
    min_rating: Optional[int] = None,
    max_rating: Optional[int] = None,
    status: Optional[Literal["PUBLIC", "PRIVATE", "UNLISTED"]] = "PUBLIC",
    tags: Optional[List[str]] = None,
    min_likes: Optional[int] = None,
    max_likes: Optional[int] = None,
    min_comments: Optional[int] = None,
    max_comments: Optional[int] = None,
    liked_by: Optional[str] = None,
    commented_by: Optional[str] = None,
    staff_pick: Optional[bool] = None,
    title_includes: Optional[str] = None,
    description_includes: Optional[str] = None,
    artists_includes: Optional[str] = None,
    author_includes: Optional[str] = None,
    sonolus_handle_is: Optional[int] = None,
    sort_by: Literal[
        "created_at",
        "rating",
        "likes",
        "comments",
        "decaying_likes",
        "abc",
        "random",
        "published_at",
    ] = "created_at",
    sort_order: Literal["desc", "asc"] = "desc",
    sonolus_id: Optional[str] = None,
    meta_includes: Optional[str] = None,
    owned_by: Optional[str] = None,
) -> tuple[
    SelectQuery[Count], SelectQuery[Union[ChartDBResponse, ChartDBResponseLiked]]
]:
    inner_select = """
        SELECT 
            c.id, 
            c.title, 
            c.artists, 
            c.description, 
            c.tags,
            c.author,
            c.staff_pick,
            c.jacket_file_hash, 
            c.music_file_hash, 
            c.chart_file_hash,
            c.preview_file_hash, 
            c.background_file_hash,
            c.background_v3_file_hash,
            c.background_v1_file_hash,
            c.status,
            c.rating, 
            c.like_count,
            c.comment_count,
            c.created_at,
            c.published_at,
            c.updated_at,
            c.log_like_score,
            c.chart_author || '#' || a.sonolus_handle AS author_full,
            a.sonolus_handle as author_handle,
            c.chart_author AS chart_design,
            c.scheduled_publish
    """

    if sonolus_id:
        inner_select += """,
            CASE WHEN cl.sonolus_id IS NULL THEN FALSE ELSE TRUE END AS liked
        """

    inner_select += """
        FROM charts c
        JOIN accounts a ON c.author = a.sonolus_id
    """

    if liked_by:
        inner_select += " JOIN chart_likes clb ON c.id = clb.chart_id"

    conditions = []
    params: List = []

    if sonolus_id:
        params.append(sonolus_id)
        inner_select += f" LEFT JOIN chart_likes cl ON c.id = cl.chart_id AND cl.sonolus_id = ${len(params)}"

    if status:
        params.append(status)
        conditions.append(f"c.status = ${len(params)}::chart_status")

    if staff_pick is not None:
        params.append(staff_pick)
        conditions.append(f"c.staff_pick = ${len(params)}::BOOL")

    if min_rating is not None:
        min_rating -= 1
        params.append(min_rating)
        conditions.append(f"c.rating > ${len(params)}")
    if max_rating is not None:
        max_rating += 1
        params.append(max_rating)
        conditions.append(f"c.rating < ${len(params)}")

    if tags:
        params.append(tags)
        conditions.append(f"c.tags @> ${len(params)}::text[]")

    if min_likes is not None:
        params.append(min_likes)
        conditions.append(f"c.like_count >= ${len(params)}")
    if max_likes is not None:
        params.append(max_likes)
        conditions.append(f"c.like_count <= ${len(params)}")

    if min_comments is not None:
        params.append(min_comments)
        conditions.append(f"c.comment_count >= ${len(params)}")
    if max_comments is not None:
        params.append(max_comments)
        conditions.append(f"c.comment_count <= ${len(params)}")

    if liked_by:
        params.append(liked_by)
        conditions.append(f"clb.sonolus_id = ${len(params)}")
    if commented_by:
        params.append(commented_by)
        inner_select += f"""
            JOIN (
                SELECT DISTINCT chart_id
                FROM comments
                WHERE commenter = ${len(params)}
            ) cmt ON c.id = cmt.chart_id
        """
    if owned_by:
        params.append(owned_by)
        conditions.append(f"c.author = ${len(params)}")
    elif sonolus_handle_is:
        params.append(sonolus_handle_is)
        conditions.append(f"a.sonolus_handle = ${len(params)}")

    if title_includes:
        params.append(f"%{title_includes.lower()}%")
        conditions.append(f"LOWER(c.title) LIKE ${len(params)}")
    if description_includes:
        params.append(f"%{description_includes.lower()}%")
        conditions.append(f"LOWER(c.description) LIKE ${len(params)}")
    if artists_includes:
        params.append(f"%{artists_includes.lower()}%")
        conditions.append(f"LOWER(c.artists) LIKE ${len(params)}")
    if author_includes:
        params.append(f"%{author_includes.lower()}%")
        conditions.append(
            f"LOWER(c.chart_author || '#' || a.sonolus_handle) LIKE ${len(params)}"
        )
    if meta_includes:
        params.append(f"%{meta_includes.lower()}%")
        placeholder = f"${len(params)}"
        conditions.append(
            f"(LOWER(c.title) LIKE {placeholder} "
            f"OR LOWER(c.description) LIKE {placeholder} "
            f"OR LOWER(c.chart_author || '#' || a.sonolus_handle) LIKE {placeholder} "
            f"OR LOWER(c.artists) LIKE {placeholder})"
        )

    if conditions:
        inner_select += " WHERE " + " AND ".join(conditions)

    W_LIKE = 3
    W_COMMENT = 4
    W_STAFF = 30
    GRAVITY = 0.35

    score_expr = f"(like_count * {W_LIKE} + comment_count * {W_COMMENT} + (CASE WHEN staff_pick THEN {W_STAFF} ELSE 0 END))"

    decaying_score_sql = f"""(
        ({score_expr}) 
        /
        POWER((EXTRACT(EPOCH FROM (NOW() - COALESCE(published_at, created_at))) / 3600) + 2, {GRAVITY})
    )"""

    sort_column = {
        "created_at": "created_at",
        "published_at": "published_at",
        "rating": "rating",
        "likes": "like_count",
        "comments": "comment_count",
        "decaying_likes": decaying_score_sql,
        "abc": "title",
        "random": "RANDOM()",
    }.get(sort_by, "created_at")

    sort_order_sql = "DESC" if sort_order.lower() == "desc" else "ASC"

    filter_published_at_null = (
        "AND published_at IS NOT NULL" if sort_column == "published_at" else ""
    )

    query = f"""
        WITH chart_data AS (
            {inner_select}
        )
        SELECT *
        FROM chart_data
        WHERE 1=1 {filter_published_at_null}
        ORDER BY {sort_column} {sort_order_sql}
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
    """

    count_query = f"""
        WITH chart_data AS (
            {inner_select}
        )
        SELECT COUNT(*) AS total_count FROM chart_data
    """

    count_params = tuple(params)

    data_params = tuple(params) + (items_per_page, page * items_per_page)

    return (
        SelectQuery(Count, count_query, *count_params),
        SelectQuery(
            ChartDBResponse if not sonolus_id else ChartDBResponseLiked,
            query,
            *data_params,
        ),
    )


def get_random_charts(
    return_count: int,
    sonolus_id: Optional[str] = None,
    staff_pick: Optional[bool] = None,
) -> SelectQuery[Union[ChartDBResponse, ChartDBResponseLiked]]:

    base_query = """
        SELECT 
            c.id,
            c.title,
            c.author,
            c.artists,
            c.staff_pick,
            c.description,
            c.tags,
            c.jacket_file_hash,
            c.music_file_hash,
            c.chart_file_hash,
            c.preview_file_hash,
            c.background_file_hash,
            c.background_v3_file_hash,
            c.background_v1_file_hash,
            c.status,
            c.rating,
            c.like_count,
            c.comment_count,
            c.created_at,
            c.published_at,
            c.updated_at,
            c.chart_author || '#' || a.sonolus_handle AS author_full,
            a.sonolus_handle as author_handle,
            c.chart_author AS chart_design,
            c.scheduled_publish
    """

    params: list = [return_count]

    if sonolus_id:
        base_query += (
            ", CASE WHEN cl.sonolus_id IS NULL THEN FALSE ELSE TRUE END AS liked"
        )

    base_query += """
        FROM charts c
        JOIN accounts a ON c.author = a.sonolus_id
    """

    if sonolus_id:
        params.append(sonolus_id)
        base_query += f"""
            LEFT JOIN chart_likes cl ON c.id = cl.chart_id AND cl.sonolus_id = ${len(params)}
        """

    base_query += " WHERE c.status = 'PUBLIC'"

    if staff_pick is not None:
        params.append(staff_pick)
        base_query += f" AND c.staff_pick = ${len(params)}::bool"

    base_query += " ORDER BY RANDOM() LIMIT $1"

    if sonolus_id:
        return SelectQuery(ChartDBResponseLiked, base_query, *params)
    return SelectQuery(ChartDBResponse, base_query, *params)


def get_chart_by_id(
    chart_id: str, sonolus_id: Optional[str] = None
) -> SelectQuery[Union[ChartByID, ChartByIDLiked]]:
    """
    Generate a query to get a chart by its ID.
    If sonolus_id is provided, also return whether this user has liked the chart.
    """
    params = [chart_id]

    if sonolus_id:
        params.append(sonolus_id)
        query = """
            SELECT 
                c.*,
                c.chart_author || '#' || a.sonolus_handle AS author_full,
                (cl.sonolus_id IS NOT NULL) AS liked,
                c.chart_author AS chart_design,
                a.sonolus_handle as author_handle
            FROM charts c
            JOIN accounts a ON c.author = a.sonolus_id
            LEFT JOIN chart_likes cl 
                ON c.id = cl.chart_id AND cl.sonolus_id = $2
            WHERE c.id = $1;
        """
        return SelectQuery(ChartByIDLiked, query, *params)
    else:
        query = """
            SELECT 
                c.*,
                c.chart_author || '#' || a.sonolus_handle AS author_full,
                c.chart_author AS chart_design,
                a.sonolus_handle as author_handle
            FROM charts c
            JOIN accounts a ON c.author = a.sonolus_id
            WHERE c.id = $1;
        """
        return SelectQuery(ChartByID, query, *params)


def get_chart_by_id_batch(chart_ids: list[str]) -> SelectQuery[ChartByID]:
    query = """
        SELECT 
            c.*,
            c.chart_author || '#' || a.sonolus_handle AS author_full,
            c.chart_author AS chart_design,
            a.sonolus_handle as author_handle
        FROM charts c
        JOIN accounts a ON c.author = a.sonolus_id
        WHERE c.id = ANY($1::text[]);
    """

    return SelectQuery(ChartByID, query, chart_ids)


def delete_chart(
    chart_id: str, sonolus_id: str = None, confirm_change: bool = False
) -> SelectQuery[ChartDBResponse]:
    if not confirm_change:
        raise ValueError(
            "Deletion not confirmed. Ensure you are deleting the old files from S3 to ensure there is no hanging files."
        )
    if not sonolus_id:
        return SelectQuery(
            ChartDBResponse,
            """
                DELETE FROM charts
                USING accounts a
                WHERE charts.id = $1
                AND charts.author = a.sonolus_id
                RETURNING
                    charts.*,
                    charts.chart_author AS chart_design,
                    a.sonolus_handle AS author_handle;
            """,
            chart_id,
        )
    else:
        return SelectQuery(
            ChartDBResponse,
            """
                DELETE FROM charts
                USING accounts a
                WHERE charts.id = $1
                AND charts.author = $2
                AND charts.author = a.sonolus_id
                RETURNING
                    charts.*,
                    charts.chart_author AS chart_design,
                    a.sonolus_handle AS author_handle;
            """,
            chart_id,
            sonolus_id,
        )


def update_metadata(
    chart_id: str,
    chart_author: Optional[str] = None,
    rating: Optional[Union[int, float, Decimal]] = None,
    description: Optional[str] = None,
    title: Optional[str] = None,
    artists: Optional[str] = None,
    tags: Optional[List[str]] = None,
    update_none_description: bool = False,
) -> ExecutableQuery:
    if not any(
        [
            rating,
            description,
            title,
            artists,
            tags,
            chart_author,
            update_none_description,
        ]
    ):
        raise ValueError("At least one field must be updated.")

    if type(rating) == int:
        rating = float(rating)

    set_fields = []
    args = []

    def add_field(field_name: str, value):
        args.append(value)
        set_fields.append(f"{field_name} = ${len(args)}")

    if rating is not None:
        add_field("rating", rating)
    if chart_author is not None:
        add_field("chart_author", chart_author)
    if description is not None:
        add_field("description", description)
    elif update_none_description:
        set_fields.append("description = NULL")
    if title is not None:
        add_field("title", title)
    if artists is not None:
        add_field("artists", artists)
    if tags is not None:
        add_field("tags", tags)

    set_fields.append("updated_at = CURRENT_TIMESTAMP")

    set_clause = ", ".join(set_fields)

    args.append(chart_id)
    query = f"""
        UPDATE charts
        SET {set_clause}
        WHERE id = ${len(args)};
    """

    return ExecutableQuery(query, *args)


def update_file_hash(
    chart_id: str,
    jacket_hash: Optional[str] = None,
    v1_hash: Optional[str] = None,
    v3_hash: Optional[str] = None,
    music_hash: Optional[str] = None,
    chart_hash: Optional[str] = None,
    preview_hash: Optional[str] = None,
    background_hash: Optional[str] = None,
    confirm_change: bool = False,
    update_none_preview: bool = False,
    update_none_background: bool = False,
) -> ExecutableQuery:
    if not confirm_change:
        raise ValueError(
            "File hash change is not confirmed. Ensure you are deleting the old files from S3 to avoid dangling files."
        )
    if jacket_hash:
        if not (v1_hash and v3_hash):
            raise ValueError("Must regenerate v1/v3 on jacket change")

    set_fields = []
    args = []

    def add_field(field_name: str, value):
        args.append(value)
        set_fields.append(f"{field_name} = ${len(args)}")

    if jacket_hash is not None:
        add_field("jacket_file_hash", jacket_hash)
    if v1_hash is not None:
        add_field("background_v1_file_hash", v1_hash)
    if v3_hash is not None:
        add_field("background_v3_file_hash", v3_hash)
    if music_hash is not None:
        add_field("music_file_hash", music_hash)
    if chart_hash is not None:
        add_field("chart_file_hash", chart_hash)
    if preview_hash is not None:
        add_field("preview_file_hash", preview_hash)
    elif update_none_preview:
        set_fields.append("preview_file_hash = NULL")
    if background_hash is not None:
        add_field("background_file_hash", background_hash)
    elif update_none_background:
        set_fields.append("background_file_hash = NULL")

    set_fields.append("updated_at = CURRENT_TIMESTAMP")

    if not set_fields:
        raise ValueError("At least one file hash must be updated.")

    set_clause = ", ".join(set_fields)

    args.append(chart_id)
    query = f"""
        UPDATE charts
        SET {set_clause}
        WHERE id = ${len(args)};
    """

    return ExecutableQuery(query, *args)


def add_like(chart_id: str, sonolus_id: str) -> ExecutableQuery:
    return ExecutableQuery(
        """
            INSERT INTO chart_likes (chart_id, sonolus_id, created_at)
            SELECT $1, $2, CURRENT_TIMESTAMP
            WHERE EXISTS (
                SELECT 1 FROM charts
                WHERE id = $1
                AND (
                    status IN ('UNLISTED', 'PUBLIC')
                    OR (status = 'PRIVATE' AND author = $2)
                )
            )
            ON CONFLICT DO NOTHING;
        """,
        chart_id,
        sonolus_id,
    )


def set_staff_pick(chart_id: str, value: bool) -> SelectQuery[ChartDBResponse]:
    return SelectQuery(
        ChartDBResponse,
        """
        WITH updated AS (
            UPDATE charts
            SET staff_pick = $2::bool,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
            RETURNING id, staff_pick
        )
        SELECT 
            charts.*, 
            charts.chart_author AS chart_design,
            charts.chart_author || '#' || accounts.sonolus_handle AS author_full,
            accounts.sonolus_handle AS author_handle
        FROM charts
        JOIN updated ON charts.id = updated.id
        JOIN accounts ON charts.author = accounts.sonolus_id;
        """,
        chart_id,
        value,
    )


def remove_like(chart_id: str, sonolus_id: str) -> ExecutableQuery:
    return ExecutableQuery(
        """
            DELETE FROM chart_likes
            WHERE chart_id = $1
            AND sonolus_id = $2
            AND EXISTS (
                SELECT 1 FROM charts
                WHERE id = $1
                AND (
                    status IN ('UNLISTED', 'PUBLIC')
                    OR (status = 'PRIVATE' AND author = $2)
                )
            );
        """,
        chart_id,
        sonolus_id,
    )


def update_status(
    chart_id: str,
    status: Literal["PUBLIC", "UNLISTED", "PRIVATE"],
    sonolus_id: Optional[str] = None,
) -> SelectQuery[ChartDBResponse]:
    if sonolus_id:
        return SelectQuery(
            ChartDBResponse,
            """
                WITH updated AS (
                    UPDATE charts
                    SET 
                        status = $1::chart_status, 
                        updated_at = CURRENT_TIMESTAMP,
                        published_at = CASE 
                            WHEN $1::chart_status = 'PUBLIC' AND published_at IS NULL THEN CURRENT_TIMESTAMP
                            ELSE published_at
                        END
                    WHERE id = $2 AND author = $3
                    RETURNING id, published_at, status
                )
                SELECT 
                    charts.*, 
                    chart_author AS chart_design, 
                    (updated.published_at IS DISTINCT FROM charts.published_at) AS is_first_publish,
                    chart_author || '#' || accounts.sonolus_handle AS author_full,
                    accounts.sonolus_handle AS author_handle
                FROM charts
                JOIN updated ON charts.id = updated.id
                JOIN accounts ON charts.author = accounts.sonolus_id;
            """,
            status,
            chart_id,
            sonolus_id,
        )
    else:
        return SelectQuery(
            ChartDBResponse,
            """
                WITH updated AS (
                    UPDATE charts
                    SET 
                        status = $1::chart_status, 
                        updated_at = CURRENT_TIMESTAMP,
                        published_at = CASE 
                            WHEN $1::chart_status = 'PUBLIC' AND published_at IS NULL THEN CURRENT_TIMESTAMP
                            ELSE published_at
                        END
                    WHERE id = $2
                    RETURNING id, published_at, status
                )
                SELECT 
                    charts.*, 
                    chart_author AS chart_design, 
                    (updated.published_at IS DISTINCT FROM charts.published_at) AS is_first_publish,
                    chart_author || '#' || accounts.sonolus_handle AS author_full,
                    accounts.sonolus_handle AS author_handle
                FROM charts
                JOIN updated ON charts.id = updated.id
                JOIN accounts ON charts.author = accounts.sonolus_id;
            """,
            status,
            chart_id,
        )


def update_scheduled_publish(
    chart_id: str,
    publish_time_seconds: Optional[int],  # none to clear
    sonolus_id: Optional[str] = None,
) -> SelectQuery[ChartDBResponse]:
    if sonolus_id:
        return SelectQuery(
            ChartDBResponse,
            """
                WITH updated AS (
                    UPDATE charts
                    SET 
                        scheduled_publish = CASE
                            WHEN $1::bigint IS NULL THEN NULL
                            ELSE to_timestamp($1::double precision)
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $2 AND author = $3
                    RETURNING id, scheduled_publish
                )
                SELECT 
                    charts.*, 
                    chart_author AS chart_design, 
                    (updated.scheduled_publish IS DISTINCT FROM charts.scheduled_publish) AS schedule_changed,
                    chart_author || '#' || accounts.sonolus_handle AS author_full,
                    accounts.sonolus_handle AS author_handle
                FROM charts
                JOIN updated ON charts.id = updated.id
                JOIN accounts ON charts.author = accounts.sonolus_id;
            """,
            publish_time_seconds,
            chart_id,
            sonolus_id,
        )
    else:
        return SelectQuery(
            ChartDBResponse,
            """
                WITH updated AS (
                    UPDATE charts
                    SET 
                        scheduled_publish = CASE
                            WHEN $1::bigint IS NULL THEN NULL
                            ELSE to_timestamp($1::double precision)
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $2
                    RETURNING id, scheduled_publish
                )
                SELECT 
                    charts.*, 
                    chart_author AS chart_design, 
                    (updated.scheduled_publish IS DISTINCT FROM charts.scheduled_publish) AS schedule_changed,
                    chart_author || '#' || accounts.sonolus_handle AS author_full,
                    accounts.sonolus_handle AS author_handle
                FROM charts
                JOIN updated ON charts.id = updated.id
                JOIN accounts ON charts.author = accounts.sonolus_id;
            """,
            publish_time_seconds,
            chart_id,
        )


# trend
def fetch_chart_like_trend(chart_id: str) -> SelectQuery[ChartLikeTrend]:
    return SelectQuery(
        ChartLikeTrend,
        """
        WITH days AS (
            SELECT
                generate_series(
                    CURRENT_DATE - INTERVAL '6 days',
                    CURRENT_DATE,
                    INTERVAL '1 day'
                )::date AS day
        )
        SELECT
            d.day,
            COUNT(cl.chart_id) AS total_likes
        FROM days d
        LEFT JOIN charts ch
            ON ch.id = $1
            AND ch.status <> 'PRIVATE'
        LEFT JOIN chart_likes cl
            ON cl.chart_id = ch.id
            AND cl.created_at::date <= d.day
        GROUP BY d.day
        ORDER BY d.day ASC;
        """,
        chart_id,
    )
