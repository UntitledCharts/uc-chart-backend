import math
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.database.core import Connection, fetch_all, fetch_one

LeaderboardType = Literal[
    "arcade_score_speed",
    "accuracy_score",
    "arcade_score_no_speed",
    "rank_match",
    "least_combo_breaks",
    "least_misses",
    "perfect",
]

SPEED_MULTIPLIER_SQL = """
    (CASE
        WHEN l.speed IS NULL THEN 1.0
        WHEN FLOOR(l.speed * 10) / 10 < 1 THEN FLOOR(l.speed * 10) / 10 - 0.4
        ELSE 1 + (FLOOR(l.speed * 10) / 10 - 1) * 0.075
    END)
"""

ORDERINGS: dict[str, str] = {
    "arcade_score_speed": f"FLOOR(l.arcade_score::numeric * {SPEED_MULTIPLIER_SQL}) DESC",
    "accuracy_score": "l.accuracy_score DESC",
    "arcade_score_no_speed": "l.arcade_score DESC",
    "rank_match": "(3 * l.nperfect::bigint + 2 * l.ngreat::bigint + l.ngood::bigint) DESC",
    "least_combo_breaks": "(l.ngood::bigint + l.nmiss::bigint) ASC",
    "least_misses": "l.nmiss ASC",
    "perfect": "l.nperfect DESC",
}

RECORD_COLUMNS = """
    l.id, l.chart_id, l.submitter, l.display_name, l.engine, l.grade,
    l.nperfect, l.ngreat, l.ngood, l.nmiss, l.arcade_score, l.accuracy_score, l.speed,
    l.replay_data_hash, l.replay_config_hash, l.public_chart, l.created_at,
    c.author AS chart_owner
"""


class RecordRow(BaseModel):
    """A leaderboard record.

    Attributes:
        id: Record id.
        chart_id: Chart id.
        submitter: Submitting account id.
        display_name: Name shown on the leaderboard.
        engine: Engine the replay was played on.
        grade: Result grade.
        nperfect: Perfect judgments.
        ngreat: Great judgments.
        ngood: Good judgments.
        nmiss: Misses.
        arcade_score: Arcade score.
        accuracy_score: Accuracy score.
        speed: Playback speed, None for 1.0.
        replay_data_hash: Replay data file hash.
        replay_config_hash: Replay configuration file hash.
        public_chart: Whether the chart was public, which decides global listing.
        created_at: Submission time.
        chart_owner: Chart owner id, used in replay file keys.
    """

    id: int
    chart_id: str
    submitter: str
    display_name: str
    engine: str
    grade: str
    nperfect: int
    ngreat: int
    ngood: int
    nmiss: int
    arcade_score: int
    accuracy_score: int
    speed: Decimal | None = None
    replay_data_hash: str
    replay_config_hash: str
    public_chart: bool
    created_at: datetime
    chart_owner: str

    @property
    def replay_prefix(self) -> str:
        """Storage prefix of this record's replay files.

        Returns:
            str: Prefix ending in a slash.
        """
        return f"{self.chart_owner}/{self.chart_id}/replays/{self.submitter}/"


def speed_multiplier(speed: float | None) -> Decimal:
    """Score multiplier used to compare scores at different speeds, computed like SQL does.

    Args:
        speed: Playback speed, None for 1.0.

    Returns:
        Decimal: The exact multiplier.
    """
    if speed is None:
        return Decimal(1)
    tier = Decimal(math.floor(Decimal(str(speed)) * 10)) / 10
    if tier < 1:
        return tier - Decimal("0.4")
    return 1 + (tier - 1) * Decimal("0.075")


def effective_score(arcade_score: int, speed: float | None) -> int:
    """Speed-adjusted arcade score, rounded down like the leaderboard ordering in SQL.

    Args:
        arcade_score: Arcade score.
        speed: Playback speed.

    Returns:
        int: Adjusted score.
    """
    return math.floor(arcade_score * speed_multiplier(speed))


async def lock_submission(connection: Connection, chart_id: str, submitter: str) -> None:
    """Make other record saves of this account on this chart wait for the transaction.

    Args:
        connection: Database connection inside a transaction.
        chart_id: Chart id.
        submitter: Submitting account id.
    """
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1::text || ':' || $2::text))",
        chart_id,
        submitter,
    )


async def get_record(connection: Connection, chart_id: str, record_id: int) -> RecordRow | None:
    """Fetch a record of a chart.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        record_id: Record id.

    Returns:
        RecordRow | None: The record, or None.
    """
    return await fetch_one(
        connection,
        RecordRow,
        f"""
        SELECT {RECORD_COLUMNS} FROM leaderboards l JOIN charts c ON c.id = l.chart_id
        WHERE l.id = $1 AND l.chart_id = $2
        """,
        record_id,
        chart_id,
    )


async def get_user_record(
    connection: Connection, chart_id: str, submitter: str, lock: bool = False
) -> RecordRow | None:
    """Fetch an account's record on a chart.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        submitter: Submitting account id.
        lock: Lock the row for update.

    Returns:
        RecordRow | None: The record, or None.
    """
    return await fetch_one(
        connection,
        RecordRow,
        f"""
        SELECT {RECORD_COLUMNS} FROM leaderboards l JOIN charts c ON c.id = l.chart_id
        WHERE l.chart_id = $1 AND l.submitter = $2
        {"FOR UPDATE OF l" if lock else ""}
        """,
        chart_id,
        submitter,
    )


async def save_record(
    connection: Connection,
    chart_id: str,
    submitter: str,
    display_name: str,
    engine: str,
    grade: str,
    counts: tuple[int, int, int, int],
    arcade_score: int,
    accuracy_score: int,
    speed: float | None,
    replay_data_hash: str,
    replay_config_hash: str,
    public_chart: bool,
) -> None:
    """Insert or replace an account's record on a chart.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        submitter: Submitting account id.
        display_name: Name shown on the leaderboard.
        engine: Engine name.
        grade: Result grade.
        counts: Perfect, great, good and miss counts.
        arcade_score: Arcade score.
        accuracy_score: Accuracy score.
        speed: Playback speed.
        replay_data_hash: Replay data file hash.
        replay_config_hash: Replay configuration file hash.
        public_chart: Whether the chart is public.
    """
    perfect, great, good, miss = counts
    await connection.execute(
        """
        INSERT INTO leaderboards (
            chart_id, submitter, display_name, engine, grade, nperfect, ngreat, ngood, nmiss,
            arcade_score, accuracy_score, speed, replay_data_hash, replay_config_hash, public_chart, created_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, CURRENT_TIMESTAMP)
        ON CONFLICT (chart_id, submitter) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            engine = EXCLUDED.engine,
            grade = EXCLUDED.grade,
            nperfect = EXCLUDED.nperfect,
            ngreat = EXCLUDED.ngreat,
            ngood = EXCLUDED.ngood,
            nmiss = EXCLUDED.nmiss,
            arcade_score = EXCLUDED.arcade_score,
            accuracy_score = EXCLUDED.accuracy_score,
            speed = EXCLUDED.speed,
            replay_data_hash = EXCLUDED.replay_data_hash,
            replay_config_hash = EXCLUDED.replay_config_hash,
            public_chart = EXCLUDED.public_chart,
            created_at = EXCLUDED.created_at
        """,
        chart_id,
        submitter,
        display_name,
        engine,
        grade,
        perfect,
        great,
        good,
        miss,
        arcade_score,
        accuracy_score,
        None if speed is None else Decimal(str(speed)),
        replay_data_hash,
        replay_config_hash,
        public_chart,
    )


async def delete_record(connection: Connection, record_id: int) -> RecordRow | None:
    """Delete a record and return it.

    Args:
        connection: Database connection.
        record_id: Record id.

    Returns:
        RecordRow | None: The deleted record, or None.
    """
    return await fetch_one(
        connection,
        RecordRow,
        f"""
        WITH deleted AS (DELETE FROM leaderboards WHERE id = $1 RETURNING *)
        SELECT {RECORD_COLUMNS.replace("l.", "d.")}
        FROM deleted d JOIN charts c ON c.id = d.chart_id
        """,
        record_id,
    )


async def delete_chart_records(connection: Connection, chart_id: str) -> None:
    """Delete every record of a chart.

    Args:
        connection: Database connection.
        chart_id: Chart id.
    """
    await connection.execute("DELETE FROM leaderboards WHERE chart_id = $1", chart_id)


async def list_chart_records(
    connection: Connection,
    chart_id: str,
    leaderboard_type: LeaderboardType,
    page: int,
    page_size: int,
) -> tuple[list[RecordRow], int]:
    """List a chart's records in leaderboard order.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        leaderboard_type: Ranking to sort by.
        page: Zero-based page.
        page_size: Records per page.

    Returns:
        tuple[list[RecordRow], int]: The page and the total number of records.
    """
    total = await connection.fetchval(
        "SELECT COUNT(*) FROM leaderboards WHERE chart_id = $1", chart_id
    )
    rows = await fetch_all(
        connection,
        RecordRow,
        f"""
        SELECT {RECORD_COLUMNS} FROM leaderboards l JOIN charts c ON c.id = l.chart_id
        WHERE l.chart_id = $1
        ORDER BY {ORDERINGS[leaderboard_type]}, l.created_at ASC, l.id ASC
        LIMIT $2 OFFSET $3
        """,
        chart_id,
        page_size,
        page * page_size,
    )
    return rows, int(total or 0)


PUBLIC_RECORD_FILTER = """
    l.public_chart = true AND c.status = 'PUBLIC' AND c.deleted_at IS NULL
    AND NOT EXISTS (SELECT 1 FROM accounts owner WHERE owner.sonolus_id = c.author AND owner.deleted_at IS NOT NULL)
"""


async def list_recent_public_records(
    connection: Connection, page: int, page_size: int
) -> tuple[list[RecordRow], int]:
    """List the newest records on public charts.

    Args:
        connection: Database connection.
        page: Zero-based page.
        page_size: Records per page.

    Returns:
        tuple[list[RecordRow], int]: The page and the total number of records.
    """
    total = await connection.fetchval(
        f"SELECT COUNT(*) FROM leaderboards l JOIN charts c ON c.id = l.chart_id WHERE {PUBLIC_RECORD_FILTER}"
    )
    rows = await fetch_all(
        connection,
        RecordRow,
        f"""
        SELECT {RECORD_COLUMNS} FROM leaderboards l JOIN charts c ON c.id = l.chart_id
        WHERE {PUBLIC_RECORD_FILTER}
        ORDER BY l.created_at DESC, l.id DESC
        LIMIT $1 OFFSET $2
        """,
        page_size,
        page * page_size,
    )
    return rows, int(total or 0)


async def random_public_records(connection: Connection, count: int) -> list[RecordRow]:
    """Pick random records on public charts.

    Args:
        connection: Database connection.
        count: Records to return.

    Returns:
        list[RecordRow]: The records.
    """
    return await fetch_all(
        connection,
        RecordRow,
        f"""
        SELECT {RECORD_COLUMNS} FROM leaderboards l JOIN charts c ON c.id = l.chart_id
        WHERE {PUBLIC_RECORD_FILTER}
        ORDER BY RANDOM()
        LIMIT $1
        """,
        count,
    )
