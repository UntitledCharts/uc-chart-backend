import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from app.database.core import (
    Connection,
    QueryBuilder,
    fetch_all,
    fetch_one,
    fetch_value,
)

ChartStatus = Literal["PUBLIC", "UNLISTED", "PRIVATE"]
SortBy = Literal[
    "created_at",
    "published_at",
    "rating",
    "likes",
    "comments",
    "decaying_likes",
    "abc",
    "random",
]

FILE_COLUMNS = (
    "jacket_file_hash",
    "music_file_hash",
    "chart_file_hash",
    "preview_file_hash",
    "background_file_hash",
    "background_v1_file_hash",
    "background_v3_file_hash",
)

CHART_COLUMNS = """
    c.id,
    c.author,
    a.sonolus_handle AS author_handle,
    a.sonolus_username AS author_username,
    a.deleted_at AS account_deleted_at,
    c.chart_author AS charter,
    c.title,
    c.artists,
    c.description,
    COALESCE(c.tags, '{}') AS tags,
    c.rating,
    c.status::text AS status,
    COALESCE(c.staff_pick, false) AS staff_pick,
    c.like_count,
    c.comment_count,
    c.log_like_score,
    c.jacket_file_hash,
    c.music_file_hash,
    c.chart_file_hash,
    c.preview_file_hash,
    c.background_file_hash,
    c.background_v1_file_hash,
    c.background_v3_file_hash,
    c.created_at,
    c.updated_at,
    c.published_at,
    c.scheduled_publish,
    c.deleted_at,
    c.chart_seconds
"""

SORT_COLUMNS: dict[str, str] = {
    "created_at": "c.created_at",
    "published_at": "c.published_at",
    "rating": "c.rating",
    "likes": "c.like_count",
    "comments": "c.comment_count",
    "decaying_likes": "c.log_like_score",
    "abc": "c.title",
}


class ChartRow(BaseModel):
    """A chart with its owner's account details.

    Attributes:
        id: Chart id.
        author: Owner account id.
        author_handle: Owner's Sonolus handle.
        author_username: Owner's Sonolus display name.
        account_deleted_at: Time the owner's account was marked for deletion.
        charter: Free-text charter name.
        title: Title.
        artists: Artists.
        description: Description.
        tags: Tags.
        rating: Difficulty rating.
        status: Visibility.
        staff_pick: Whether staff picked the chart.
        like_count: Likes.
        comment_count: Comments, including deleted ones.
        log_like_score: Time-decayed like score.
        jacket_file_hash: Jacket file hash.
        music_file_hash: Audio file hash.
        chart_file_hash: Level data file hash.
        preview_file_hash: Preview audio file hash.
        background_file_hash: Uploaded background file hash.
        background_v1_file_hash: Generated v1 background hash.
        background_v3_file_hash: Generated v3 background hash.
        created_at: Upload time.
        updated_at: Last update time.
        published_at: First publish time.
        scheduled_publish: Scheduled publish time.
        deleted_at: Time the chart was soft-deleted.
        chart_seconds: Time of the last note in seconds, None until measured.
        liked: Whether the viewer liked the chart, when a viewer is known.
    """

    id: str
    author: str
    author_handle: int
    author_username: str
    account_deleted_at: datetime | None = None
    charter: str
    title: str
    artists: str | None = None
    description: str | None = None
    tags: list[str] = []
    rating: Decimal
    status: ChartStatus
    staff_pick: bool = False
    like_count: int = 0
    comment_count: int = 0
    log_like_score: float = 0.0
    jacket_file_hash: str
    music_file_hash: str
    chart_file_hash: str
    preview_file_hash: str | None = None
    background_file_hash: str | None = None
    background_v1_file_hash: str
    background_v3_file_hash: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    published_at: datetime | None = None
    scheduled_publish: datetime | None = None
    deleted_at: datetime | None = None
    chart_seconds: float | None = None
    liked: bool | None = None

    @property
    def charter_full(self) -> str:
        """Charter name with the owner's handle.

        Returns:
            str: Text like ``name#1234``.
        """
        return f"{self.charter}#{self.author_handle}"

    @property
    def hidden(self) -> bool:
        """Whether the chart or its owner is pending deletion.

        Returns:
            bool: True when soft-deleted.
        """
        return self.deleted_at is not None or self.account_deleted_at is not None


@dataclass
class ChartFilters:
    """Chart listing filters.

    Attributes:
        status: Visibility to list, None for any (own charts only).
        owned_by: Only charts of this owner.
        owner_handle: Only charts owned by, or crediting, the account with this handle.
        credited_account: Only charts crediting this account as a collaborator.
        staff_pick: Only picked (True) or unpicked (False) charts.
        min_rating: Lowest rating level, inclusive.
        max_rating: Highest rating level, inclusive.
        tags: Charts must have all of these tags.
        min_likes: Fewest likes.
        max_likes: Most likes.
        min_comments: Fewest comments.
        max_comments: Most comments.
        liked_by: Only charts this account liked.
        commented_by: Only charts this account commented on.
        title_includes: Title substring.
        description_includes: Description substring.
        artists_includes: Artists substring.
        author_includes: Substring of the owner's ``charter#handle`` or a credited collaborator's
            ``username#handle``.
        meta_includes: Substring of title, description, artists or any credit.
        sort_by: Sort key.
        sort_order: Sort direction.
    """

    status: ChartStatus | None = "PUBLIC"
    owned_by: str | None = None
    owner_handle: int | None = None
    credited_account: str | None = None
    staff_pick: bool | None = None
    min_rating: int | None = None
    max_rating: int | None = None
    tags: list[str] = field(default_factory=list)
    min_likes: int | None = None
    max_likes: int | None = None
    min_comments: int | None = None
    max_comments: int | None = None
    liked_by: str | None = None
    commented_by: str | None = None
    title_includes: str | None = None
    description_includes: str | None = None
    artists_includes: str | None = None
    author_includes: str | None = None
    meta_includes: str | None = None
    sort_by: SortBy = "created_at"
    sort_order: Literal["desc", "asc"] = "desc"


COUNT_CACHE_SECONDS = 10.0
COUNT_CACHE_SIZE = 512

CountKey = tuple[str, tuple[Any, ...]]


class CountCache:
    """Short-lived per-worker cache of listing totals that don't depend on who is asking.

    Attributes:
        entries: Totals with their expiry time, least recently stored first.
    """

    def __init__(self) -> None:
        """Create an empty cache."""
        self.entries: OrderedDict[CountKey, tuple[float, int]] = OrderedDict()

    @staticmethod
    def key(sql: str, params: tuple[Any, ...]) -> CountKey:
        """Build a hashable cache key for a count query.

        Args:
            sql: Count query.
            params: Query parameters.

        Returns:
            CountKey: The key.
        """
        return sql, tuple(tuple(value) if isinstance(value, list) else value for value in params)

    def get(self, key: CountKey) -> int | None:
        """Look up a total that hasn't expired.

        Args:
            key: Cache key.

        Returns:
            int | None: The total, or None when missing or expired.
        """
        entry = self.entries.get(key)
        if entry is None:
            return None
        expires, total = entry
        if expires < time.monotonic():
            del self.entries[key]
            return None
        return total

    def put(self, key: CountKey, total: int) -> None:
        """Store a total, evicting the oldest entries past the size limit.

        Args:
            key: Cache key.
            total: The total.
        """
        self.entries[key] = (time.monotonic() + COUNT_CACHE_SECONDS, total)
        self.entries.move_to_end(key)
        while len(self.entries) > COUNT_CACHE_SIZE:
            self.entries.popitem(last=False)


_count_cache = CountCache()

RANDOM_CACHE_SECONDS = 5.0

_random_cache: dict[tuple[int, bool | None], tuple[float, list["ChartRow"]]] = {}


def like_pattern(text: str) -> str:
    """Build a case-insensitive LIKE pattern matching a literal substring.

    Args:
        text: Substring to find.

    Returns:
        str: Pattern with LIKE wildcards escaped.
    """
    escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _liked_join(query: QueryBuilder, viewer_id: str | None) -> tuple[str, str]:
    """Build the select column and join reporting whether the viewer liked a chart.

    Args:
        query: Builder receiving the viewer parameter.
        viewer_id: Viewer account id.

    Returns:
        tuple[str, str]: Extra select text and join text.
    """
    if viewer_id is None:
        return "", ""
    placeholder = query.param(viewer_id)
    return (
        ", (viewer_like.sonolus_id IS NOT NULL) AS liked",
        f" LEFT JOIN chart_likes viewer_like ON viewer_like.chart_id = c.id AND viewer_like.sonolus_id = {placeholder}",
    )


async def get_chart(
    connection: Connection, chart_id: str, viewer_id: str | None = None
) -> ChartRow | None:
    """Fetch a chart by id, including soft-deleted ones.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        viewer_id: Viewer whose like is reported.

    Returns:
        ChartRow | None: The chart, or None.
    """
    query = QueryBuilder([chart_id])
    liked_select, liked_join = _liked_join(query, viewer_id)
    return await fetch_one(
        connection,
        ChartRow,
        f"""
        SELECT {CHART_COLUMNS}{liked_select}
        FROM charts c
        JOIN accounts a ON a.sonolus_id = c.author{liked_join}
        WHERE c.id = $1
        """,
        *query.params,
    )


async def get_charts(connection: Connection, chart_ids: list[str]) -> dict[str, ChartRow]:
    """Fetch several charts by id.

    Args:
        connection: Database connection.
        chart_ids: Chart ids.

    Returns:
        dict[str, ChartRow]: Charts keyed by id.
    """
    if not chart_ids:
        return {}
    rows = await fetch_all(
        connection,
        ChartRow,
        f"""
        SELECT {CHART_COLUMNS}
        FROM charts c
        JOIN accounts a ON a.sonolus_id = c.author
        WHERE c.id = ANY($1::text[])
        """,
        list(set(chart_ids)),
    )
    return {row.id: row for row in rows}


async def list_charts(
    connection: Connection,
    filters: ChartFilters,
    page: int,
    page_size: int,
    viewer_id: str | None = None,
) -> tuple[list[ChartRow], int]:
    """List charts matching filters, hiding soft-deleted charts and owners.

    Args:
        connection: Database connection.
        filters: Listing filters.
        page: Zero-based page.
        page_size: Charts per page.
        viewer_id: Viewer whose likes are reported.

    Returns:
        tuple[list[ChartRow], int]: The page and the total number of matches.
    """
    query = QueryBuilder()
    query.where("c.deleted_at IS NULL")
    query.where("a.deleted_at IS NULL")
    if filters.status is not None:
        query.where(f"c.status = {query.param(filters.status)}::chart_status")
    if filters.owned_by is not None:
        query.where(f"c.author = {query.param(filters.owned_by)}")
    elif filters.owner_handle is not None:
        handle = query.param(filters.owner_handle)
        query.where(
            f"""(
                c.author = ANY (ARRAY(
                    SELECT ha.sonolus_id FROM accounts ha WHERE ha.sonolus_handle = {handle}
                ))
                OR c.id = ANY (ARRAY(
                    SELECT hcc.chart_id FROM chart_collaborators hcc
                    JOIN accounts ha ON ha.sonolus_id = hcc.sonolus_id
                    WHERE ha.sonolus_handle = {handle} AND 'credit' = ANY (hcc.permissions)
                ))
            )"""
        )
    if filters.credited_account is not None:
        query.where(
            f"""EXISTS (
                SELECT 1 FROM chart_collaborators ccr
                WHERE ccr.chart_id = c.id AND ccr.sonolus_id = {query.param(filters.credited_account)}
                  AND 'credit' = ANY (ccr.permissions)
            )"""
        )
    if filters.staff_pick is not None:
        query.where(f"COALESCE(c.staff_pick, false) = {query.param(filters.staff_pick)}")
    if filters.min_rating is not None:
        query.where(f"c.rating >= {query.param(filters.min_rating)}")
    if filters.max_rating is not None:
        query.where(f"c.rating < {query.param(filters.max_rating + 1)}")
    if filters.tags:
        query.where(f"c.tags @> {query.param(filters.tags)}::text[]")
    if filters.min_likes is not None:
        query.where(f"c.like_count >= {query.param(filters.min_likes)}")
    if filters.max_likes is not None:
        query.where(f"c.like_count <= {query.param(filters.max_likes)}")
    if filters.min_comments is not None:
        query.where(f"c.comment_count >= {query.param(filters.min_comments)}")
    if filters.max_comments is not None:
        query.where(f"c.comment_count <= {query.param(filters.max_comments)}")
    if filters.liked_by is not None:
        query.where(
            f"EXISTS (SELECT 1 FROM chart_likes lb WHERE lb.chart_id = c.id AND lb.sonolus_id = {query.param(filters.liked_by)})"
        )
    if filters.commented_by is not None:
        query.where(
            f"EXISTS (SELECT 1 FROM comments cb WHERE cb.chart_id = c.id AND cb.commenter = {query.param(filters.commented_by)})"
        )
    text_filters = (
        ("lower(c.title)", filters.title_includes),
        ("lower(c.description)", filters.description_includes),
        ("lower(c.artists)", filters.artists_includes),
        ("c.credit_search", filters.author_includes),
    )
    for column, value in text_filters:
        if value:
            query.where(f"{column} LIKE {query.param(like_pattern(value))} ESCAPE '\\'")
    if filters.meta_includes:
        placeholder = query.param(like_pattern(filters.meta_includes))
        query.where(
            "("
            + " OR ".join(
                f"{column} LIKE {placeholder} ESCAPE '\\'"
                for column in (
                    "lower(c.title)",
                    "lower(c.description)",
                    "c.credit_search",
                    "lower(c.artists)",
                )
            )
            + ")"
        )
    if filters.sort_by == "published_at":
        query.where("c.published_at IS NOT NULL")
    where_clause = query.where_clause()
    count_params = tuple(query.params)
    liked_select, liked_join = _liked_join(query, viewer_id)

    direction = "DESC" if filters.sort_order == "desc" else "ASC"
    if filters.sort_by == "abc":
        direction = "ASC" if filters.sort_order == "desc" else "DESC"
    if filters.sort_by == "random":
        order = "RANDOM()"
    else:
        order = f"{SORT_COLUMNS[filters.sort_by]} {direction}, c.id {direction}"
    limit = query.param(page_size)
    offset = query.param(page * page_size)
    rows = await fetch_all(
        connection,
        ChartRow,
        f"""
        SELECT {CHART_COLUMNS}{liked_select}
        FROM charts c
        JOIN accounts a ON a.sonolus_id = c.author{liked_join}
        {where_clause}
        ORDER BY {order}
        LIMIT {limit} OFFSET {offset}
        """,
        *query.params,
    )
    if 0 < len(rows) < page_size or (page == 0 and not rows):
        return rows, page * page_size + len(rows)
    count_sql = f"""
        SELECT COUNT(*) FROM charts c
        JOIN accounts a ON a.sonolus_id = c.author
        {where_clause}
        """
    personal = (
        filters.owned_by or filters.liked_by or filters.commented_by or filters.credited_account
    )
    cache_key = None if personal else CountCache.key(count_sql, count_params)
    cached = _count_cache.get(cache_key) if cache_key else None
    if cached is not None:
        return rows, cached
    total = int(await fetch_value(connection, count_sql, *count_params))
    if cache_key:
        _count_cache.put(cache_key, total)
    return rows, total


async def random_charts(
    connection: Connection, count: int, viewer_id: str | None, staff_pick: bool | None
) -> list[ChartRow]:
    """Pick random public charts, reusing the same pick per worker for a few seconds.

    Args:
        connection: Database connection.
        count: Charts to return.
        viewer_id: Viewer whose likes are reported.
        staff_pick: Only picked (True) or unpicked (False) charts.

    Returns:
        list[ChartRow]: The charts.
    """
    key = (count, staff_pick)
    cached = _random_cache.get(key)
    if cached is not None and cached[0] >= time.monotonic():
        rows = cached[1]
    else:
        query = QueryBuilder()
        query.where("c.status = 'PUBLIC'")
        query.where("c.deleted_at IS NULL")
        query.where("a.deleted_at IS NULL")
        if staff_pick is not None:
            query.where(f"COALESCE(c.staff_pick, false) = {query.param(staff_pick)}")
        limit = query.param(count)
        rows = await fetch_all(
            connection,
            ChartRow,
            f"""
            SELECT {CHART_COLUMNS}
            FROM charts c
            JOIN accounts a ON a.sonolus_id = c.author
            {query.where_clause()}
            ORDER BY RANDOM()
            LIMIT {limit}
            """,
            *query.params,
        )
        _random_cache[key] = (time.monotonic() + RANDOM_CACHE_SECONDS, rows)
    if viewer_id is None or not rows:
        return rows
    liked = {
        record["chart_id"]
        for record in await connection.fetch(
            "SELECT chart_id FROM chart_likes WHERE sonolus_id = $1 AND chart_id = ANY($2::text[])",
            viewer_id,
            [row.id for row in rows],
        )
    }
    return [row.model_copy(update={"liked": row.id in liked}) for row in rows]


async def create_chart(
    connection: Connection,
    values: dict[str, Any],
    status: Literal["PRIVATE", "UNLISTED"],
) -> None:
    """Insert a new private or unlisted chart.

    Args:
        connection: Database connection.
        values: Column values; must include id, author and every required column.
        status: Visibility.
    """
    columns = list(values)
    params = [values[column] for column in columns]
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 2))
    await connection.execute(
        f"""
        INSERT INTO charts ({", ".join(columns)}, status, created_at, updated_at)
        VALUES ({placeholders}::chart_status, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        *params,
        status,
    )


async def set_chart_seconds(
    connection: Connection, chart_id: str, seconds: float, chart_file_hash: str
) -> None:
    """Store how long a chart's notes last unless its chart file changed or it's already stored.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        seconds: Time of the last note in seconds.
        chart_file_hash: Hash of the level data that was measured.
    """
    await connection.execute(
        """
        UPDATE charts SET chart_seconds = $2
        WHERE id = $1 AND chart_file_hash = $3 AND chart_seconds IS NULL
        """,
        chart_id,
        seconds,
        chart_file_hash,
    )


async def update_chart(connection: Connection, chart_id: str, values: dict[str, Any]) -> None:
    """Update chart columns and bump updated_at.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        values: Column values to write.
    """
    if not values:
        return
    columns = list(values)
    assignments = ", ".join(f"{column} = ${index}" for index, column in enumerate(columns, start=2))
    await connection.execute(
        f"UPDATE charts SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = $1",
        chart_id,
        *(values[column] for column in columns),
    )


async def soft_delete(connection: Connection, chart_id: str) -> datetime | None:
    """Mark a chart deleted unless it already is.

    Args:
        connection: Database connection.
        chart_id: Chart id.

    Returns:
        datetime | None: The deletion time, or None when the chart doesn't exist or already was deleted.
    """
    return await fetch_value(
        connection,
        """
        UPDATE charts SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = $1 AND deleted_at IS NULL
        RETURNING deleted_at
        """,
        chart_id,
    )


async def restore(connection: Connection, chart_id: str) -> bool:
    """Clear a chart's soft-deletion.

    Args:
        connection: Database connection.
        chart_id: Chart id.

    Returns:
        bool: Whether a deleted chart was restored.
    """
    restored = await fetch_value(
        connection,
        """
        UPDATE charts SET deleted_at = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE id = $1 AND deleted_at IS NOT NULL
        RETURNING true
        """,
        chart_id,
    )
    return bool(restored)


async def hard_delete(
    connection: Connection, chart_id: str, grace_days: int | None = None
) -> tuple[bool, str | None]:
    """Delete a chart row and everything cascading from it.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        grace_days: Only delete a chart soft-deleted at least this many days ago, None to delete
            it regardless.

    Returns:
        tuple[bool, str | None]: Whether a row was deleted, and its owner id.
    """
    if grace_days is None:
        record = await connection.fetchrow(
            "DELETE FROM charts WHERE id = $1 RETURNING author", chart_id
        )
    else:
        record = await connection.fetchrow(
            """
            DELETE FROM charts
            WHERE id = $1 AND deleted_at IS NOT NULL
              AND deleted_at <= CURRENT_TIMESTAMP - make_interval(days => $2)
            RETURNING author
            """,
            chart_id,
            grace_days,
        )
    if record is None:
        return False, None
    return True, record["author"]


async def list_charts_to_purge(connection: Connection, grace_days: int, limit: int) -> list[str]:
    """List soft-deleted charts whose grace period is over.

    Args:
        connection: Database connection.
        grace_days: Grace period in days.
        limit: Most charts returned.

    Returns:
        list[str]: Chart ids.
    """
    records = await connection.fetch(
        """
        SELECT id FROM charts
        WHERE deleted_at IS NOT NULL AND deleted_at <= CURRENT_TIMESTAMP - make_interval(days => $1)
        ORDER BY deleted_at
        LIMIT $2
        """,
        grace_days,
        limit,
    )
    return [record["id"] for record in records]


async def set_staff_pick(connection: Connection, chart_id: str, value: bool) -> bool | None:
    """Set a chart's staff pick flag.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        value: New flag.

    Returns:
        bool | None: The previous flag, or None when the chart doesn't exist.
    """
    return await fetch_value(
        connection,
        """
        UPDATE charts c SET staff_pick = $2, updated_at = CURRENT_TIMESTAMP
        FROM (SELECT id, COALESCE(staff_pick, false) AS staff_pick FROM charts WHERE id = $1 FOR UPDATE) previous
        WHERE c.id = previous.id
        RETURNING previous.staff_pick
        """,
        chart_id,
        value,
    )


async def set_rating(connection: Connection, chart_id: str, rating: Decimal) -> Decimal | None:
    """Set a chart's rating.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        rating: New rating.

    Returns:
        Decimal | None: The previous rating, or None when the chart doesn't exist.
    """
    previous = await fetch_one(
        connection,
        _Rating,
        """
        UPDATE charts c SET rating = $2, updated_at = CURRENT_TIMESTAMP
        FROM (SELECT id, rating FROM charts WHERE id = $1 FOR UPDATE) previous
        WHERE c.id = previous.id
        RETURNING previous.rating AS rating
        """,
        chart_id,
        rating,
    )
    return None if previous is None else previous.rating


class _Rating(BaseModel):
    """A rating value.

    Attributes:
        rating: The rating.
    """

    rating: Decimal


class GameplayFiles(BaseModel):
    """A chart's gameplay files and visibility.

    Attributes:
        chart_file_hash: Level data file hash.
        music_file_hash: Audio file hash.
        status: Visibility.
    """

    chart_file_hash: str
    music_file_hash: str
    status: ChartStatus


async def lock_gameplay(connection: Connection, chart_id: str) -> GameplayFiles | None:
    """Keep a chart from being edited until the transaction ends and read its gameplay files.

    Args:
        connection: Database connection inside a transaction.
        chart_id: Chart id.

    Returns:
        GameplayFiles | None: The files and visibility, or None when the chart doesn't exist.
    """
    return await fetch_one(
        connection,
        GameplayFiles,
        """
        SELECT chart_file_hash, music_file_hash, status::text AS status
        FROM charts WHERE id = $1
        FOR SHARE
        """,
        chart_id,
    )


class StatusChange(BaseModel):
    """Result of a visibility change.

    Attributes:
        previous_status: Status before the change.
        status: Status after the change.
        first_publish: Whether the chart became public for the first time.
        previous_scheduled_publish: Scheduled publish time cleared by going public, if any.
    """

    previous_status: ChartStatus
    status: ChartStatus
    first_publish: bool
    previous_scheduled_publish: datetime | None


async def set_status(
    connection: Connection, chart_id: str, status: ChartStatus
) -> StatusChange | None:
    """Change a chart's visibility and keep leaderboards and promotions in sync.

    Args:
        connection: Database connection inside a transaction.
        chart_id: Chart id.
        status: New visibility.

    Returns:
        StatusChange | None: What changed, or None when the chart doesn't exist.
    """
    change = await fetch_one(
        connection,
        StatusChange,
        """
        WITH previous AS (
            SELECT status, published_at, scheduled_publish FROM charts WHERE id = $1 FOR UPDATE
        )
        UPDATE charts c
        SET status = $2::chart_status,
            updated_at = CURRENT_TIMESTAMP,
            published_at = CASE WHEN $2 = 'PUBLIC' AND c.published_at IS NULL THEN CURRENT_TIMESTAMP ELSE c.published_at END,
            scheduled_publish = CASE WHEN $2 = 'PUBLIC' THEN NULL ELSE c.scheduled_publish END
        FROM previous
        WHERE c.id = $1
        RETURNING previous.status::text AS previous_status, c.status::text AS status,
                  (previous.published_at IS NULL AND c.published_at IS NOT NULL) AS first_publish,
                  CASE WHEN $2 = 'PUBLIC' THEN previous.scheduled_publish END
                      AS previous_scheduled_publish
        """,
        chart_id,
        status,
    )
    if change is None:
        return None
    await connection.execute(
        "UPDATE leaderboards SET public_chart = $2 WHERE chart_id = $1",
        chart_id,
        status == "PUBLIC",
    )
    if status != "PUBLIC":
        await connection.execute(
            """
            WITH cancelled AS (
                UPDATE promotions SET cancelled = true, ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)
                WHERE chart_id = $1 AND cancelled = false AND ended_at IS NULL
                RETURNING id
            )
            DELETE FROM promotion_views WHERE promotion_id IN (SELECT id FROM cancelled)
            """,
            chart_id,
        )
    return change


async def lock_status(connection: Connection, chart_id: str) -> ChartStatus | None:
    """Keep a chart's visibility from changing until the transaction ends and read it.

    Args:
        connection: Database connection inside a transaction.
        chart_id: Chart id.

    Returns:
        ChartStatus | None: The visibility, or None when the chart doesn't exist.
    """
    return await fetch_value(
        connection, "SELECT status::text FROM charts WHERE id = $1 FOR UPDATE", chart_id
    )


async def set_scheduled_publish(
    connection: Connection, chart_id: str, publish_at: datetime | None
) -> tuple[bool, datetime | None]:
    """Set or clear a chart's scheduled publish time.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        publish_at: New publish time, or None to cancel.

    Returns:
        tuple[bool, datetime | None]: Whether the chart exists, and the previous publish time.
    """
    record = await connection.fetchrow(
        """
        UPDATE charts c SET scheduled_publish = $2, updated_at = CURRENT_TIMESTAMP
        FROM (SELECT id, scheduled_publish FROM charts WHERE id = $1 FOR UPDATE) previous
        WHERE c.id = previous.id
        RETURNING previous.scheduled_publish AS previous
        """,
        chart_id,
        publish_at,
    )
    if record is None:
        return False, None
    return True, record["previous"]


async def publish_due_charts(connection: Connection, limit: int) -> list[tuple[str, bool]]:
    """Publish charts whose scheduled time has passed.

    Args:
        connection: Database connection inside a transaction.
        limit: Most charts published.

    Returns:
        list[tuple[str, bool]]: Chart ids and whether each was published for the first time.
    """
    records = await connection.fetch(
        """
        WITH due AS (
            SELECT id, published_at FROM charts
            WHERE scheduled_publish IS NOT NULL
              AND scheduled_publish <= CURRENT_TIMESTAMP
              AND status <> 'PUBLIC'
              AND deleted_at IS NULL
            ORDER BY scheduled_publish
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        UPDATE charts c
        SET status = 'PUBLIC',
            scheduled_publish = NULL,
            updated_at = CURRENT_TIMESTAMP,
            published_at = COALESCE(c.published_at, CURRENT_TIMESTAMP)
        FROM due
        WHERE c.id = due.id
        RETURNING c.id, (due.published_at IS NULL) AS first_publish
        """,
        limit,
    )
    published = [(record["id"], bool(record["first_publish"])) for record in records]
    if published:
        await connection.execute(
            "UPDATE leaderboards SET public_chart = true WHERE chart_id = ANY($1::text[])",
            [chart_id for chart_id, _ in published],
        )
    return published


async def add_like(connection: Connection, chart_id: str, sonolus_id: str) -> None:
    """Like a chart the account can see.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        sonolus_id: Liking account.
    """
    await connection.execute(
        """
        INSERT INTO chart_likes (chart_id, sonolus_id, created_at)
        SELECT $1, $2, CURRENT_TIMESTAMP
        WHERE EXISTS (
            SELECT 1 FROM charts
            WHERE id = $1 AND (status IN ('UNLISTED', 'PUBLIC') OR (status = 'PRIVATE' AND author = $2))
        )
        ON CONFLICT DO NOTHING
        """,
        chart_id,
        sonolus_id,
    )


async def remove_like(connection: Connection, chart_id: str, sonolus_id: str) -> None:
    """Remove a like from a chart the account can see.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        sonolus_id: Account removing its like.
    """
    await connection.execute(
        """
        DELETE FROM chart_likes
        WHERE chart_id = $1 AND sonolus_id = $2
          AND EXISTS (
              SELECT 1 FROM charts
              WHERE id = $1 AND (status IN ('UNLISTED', 'PUBLIC') OR (status = 'PRIVATE' AND author = $2))
          )
        """,
        chart_id,
        sonolus_id,
    )


async def like_trend(connection: Connection, chart_id: str) -> list[int]:
    """Total likes at the end of each of the last 7 days, oldest first.

    Args:
        connection: Database connection.
        chart_id: Chart id.

    Returns:
        list[int]: Seven running totals.
    """
    records = await connection.fetch(
        """
        WITH days AS (
            SELECT generate_series(CURRENT_DATE - INTERVAL '6 days', CURRENT_DATE, INTERVAL '1 day')::date AS day
        )
        SELECT d.day, COUNT(cl.chart_id) AS total
        FROM days d
        LEFT JOIN charts ch ON ch.id = $1 AND ch.status <> 'PRIVATE'
        LEFT JOIN chart_likes cl ON cl.chart_id = ch.id AND cl.created_at::date <= d.day
        GROUP BY d.day
        ORDER BY d.day
        """,
        chart_id,
    )
    return [int(record["total"]) for record in records]


async def comment_trend(connection: Connection, chart_id: str) -> list[int]:
    """Total comments at the end of each of the last 7 days, oldest first.

    Args:
        connection: Database connection.
        chart_id: Chart id.

    Returns:
        list[int]: Seven running totals.
    """
    records = await connection.fetch(
        """
        WITH days AS (
            SELECT generate_series(CURRENT_DATE - INTERVAL '6 days', CURRENT_DATE, INTERVAL '1 day')::date AS day
        )
        SELECT d.day, COUNT(cm.id) AS total
        FROM days d
        LEFT JOIN charts ch ON ch.id = $1 AND ch.status <> 'PRIVATE'
        LEFT JOIN comments cm ON cm.chart_id = ch.id AND cm.created_at::date <= d.day
        GROUP BY d.day
        ORDER BY d.day
        """,
        chart_id,
    )
    return [int(record["total"]) for record in records]
