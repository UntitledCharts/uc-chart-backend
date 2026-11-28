import argparse
import asyncio
import os
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass

import aiohttp
import asyncpg
from botocore.exceptions import BotoCoreError, ClientError

from app.database import staff_actions as staff_actions_db
from app.database.core import Database, fetch_value
from app.services import audio, conversion
from app.services.storage import Storage, StorageError
from app.settings import Config, Constants
from scripts._common import (
    add_config_argument,
    add_constants_argument,
    confirm,
    read_config,
    read_constants,
    small_pool,
)

DEFAULT_CONCURRENCY = 8
PROGRESS_SECONDS = 5.0
REASON_SONG = "song"
REASON_CHART = "chart"
STORAGE_ERRORS = (
    ClientError,
    BotoCoreError,
    aiohttp.ClientError,
    StorageError,
    OSError,
)

CHARTS_QUERY = """
    SELECT id, author, status::text AS status, title, music_file_hash, chart_file_hash,
        chart_seconds, scheduled_publish IS NOT NULL AS scheduled
    FROM charts
    WHERE deleted_at IS NULL
    ORDER BY id
"""
DELETE_QUERY = """
    UPDATE charts SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
    WHERE id = $1
      AND deleted_at IS NULL
      AND music_file_hash IS NOT DISTINCT FROM $2
      AND chart_file_hash IS NOT DISTINCT FROM $3
      AND ($4::bool OR status = 'PUBLIC' OR scheduled_publish IS NOT NULL)
    RETURNING id
"""
STORE_SECONDS_QUERY = """
    UPDATE charts SET chart_seconds = $2
    WHERE id = $1 AND chart_file_hash = $3 AND chart_seconds IS NULL
"""


class CheckFailure(Exception):
    """Raised when a chart can't be checked.

    Attributes:
        reason: Failure category shown in the summary.
    """

    def __init__(self, reason: str, detail: str) -> None:
        """Create a failure.

        Args:
            reason: Failure category shown in the summary.
            detail: What went wrong.
        """
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


@dataclass(frozen=True)
class Chart:
    """A chart to check.

    Attributes:
        chart_id: Chart id.
        owner: Owner's Sonolus id, the first segment of the chart's storage keys.
        status: Visibility.
        title: Title.
        music_hash: SHA-1 of the music file.
        chart_hash: SHA-1 of the level data.
        chart_seconds: Stored time of the last note, None until measured.
        scheduled: Whether the chart is waiting for a scheduled release.
    """

    chart_id: str
    owner: str
    status: str
    title: str
    music_hash: str | None
    chart_hash: str | None
    chart_seconds: float | None
    scheduled: bool

    @property
    def going_public(self) -> bool:
        """Whether the chart is public or scheduled to become public.

        Returns:
            bool: True when the public chart rules apply to it.
        """
        return self.status == "PUBLIC" or self.scheduled


@dataclass(frozen=True)
class Verdict:
    """What checking a chart found.

    Attributes:
        chart: The chart.
        reason: REASON_SONG or REASON_CHART when the chart breaks a rule, else None.
        seconds: The length that broke the rule.
        measured: Last-note time measured for a chart without a stored one.
        failure: Why the chart couldn't be checked, or None.
    """

    chart: Chart
    reason: str | None = None
    seconds: float | None = None
    measured: float | None = None
    failure: str | None = None


class Scan:
    """Checks charts against the song length rule, and public or scheduled ones against the chart one.

    Attributes:
        storage: Open storage client.
        constants: Validated constants.yml.
        limit: Largest decompressed level data size accepted.
        memory_limit: Largest memory one measuring process may use, in bytes.
        slots: Charts checked at once.
        threads: Threads running ffprobe.
        processes: Processes reading level data.
    """

    def __init__(
        self,
        storage: Storage,
        constants: Constants,
        limit: int,
        memory_limit: int,
        concurrency: int,
    ) -> None:
        """Create a scan.

        Args:
            storage: Open storage client.
            constants: Validated constants.yml.
            limit: Largest decompressed level data size accepted.
            memory_limit: Largest memory one measuring process may use, in bytes.
            concurrency: Charts checked at once.
        """
        self.storage = storage
        self.constants = constants
        self.limit = limit
        self.memory_limit = memory_limit
        self.slots = asyncio.Semaphore(concurrency)
        self.threads = ThreadPoolExecutor(max_workers=concurrency)
        self.processes = self._pool()

    def _pool(self) -> ProcessPoolExecutor:
        """Start measuring processes capped like the API's conversion children.

        Returns:
            ProcessPoolExecutor: The pool.
        """
        return ProcessPoolExecutor(
            max_workers=os.cpu_count() or 1,
            initializer=conversion.confine,
            initargs=(self.memory_limit, os.getpid()),
        )

    def close(self) -> None:
        """Stop the worker threads and processes."""
        self.threads.shutdown(wait=False, cancel_futures=True)
        self.processes.shutdown(wait=False, cancel_futures=True)

    async def download(self, chart: Chart, file_hash: str, kind: str) -> bytes:
        """Download one of a chart's files.

        Args:
            chart: The chart.
            file_hash: File hash.
            kind: What the file is, for failures.

        Returns:
            bytes: The file.
        """
        key = f"{chart.owner}/{chart.chart_id}/{file_hash}"
        try:
            stored = await self.storage.get(key)
        except STORAGE_ERRORS as error:
            raise CheckFailure(f"{kind} download failed", f"{key}: {error!r}") from None
        if stored is None:
            raise CheckFailure(f"{kind} file missing", key)
        return stored[0]

    async def chart_seconds(self, chart: Chart, file_hash: str) -> float:
        """Measure when a chart's last note plays.

        Args:
            chart: The chart.
            file_hash: Level data hash.

        Returns:
            float: Time of the last note in seconds.
        """
        data = await self.download(chart, file_hash, REASON_CHART)
        pool = self.processes
        try:
            return await asyncio.get_running_loop().run_in_executor(
                pool, conversion.level_data_seconds, data, self.limit
            )
        except BrokenProcessPool:
            if self.processes is pool:
                self.processes = self._pool()
                pool.shutdown(wait=False, cancel_futures=True)
            raise CheckFailure("chart unreadable", "the measuring process crashed") from None
        except Exception as error:
            raise CheckFailure("chart unreadable", repr(error)) from None

    async def song_seconds(self, chart: Chart, file_hash: str) -> float:
        """Measure how long a chart's song plays.

        Args:
            chart: The chart.
            file_hash: Music file hash.

        Returns:
            float: Duration in seconds.
        """
        data = await self.download(chart, file_hash, REASON_SONG)
        seconds = await asyncio.get_running_loop().run_in_executor(
            self.threads, audio.duration_seconds, data, self.constants.audio
        )
        if seconds is None:
            raise CheckFailure("song unreadable", "ffprobe couldn't read its duration")
        return seconds

    async def check(self, chart: Chart) -> Verdict:
        """Check a chart against both rules, so a probe failing for one rule doesn't skip the other.

        Args:
            chart: The chart.

        Returns:
            Verdict: What was found.
        """
        async with self.slots:
            measured: float | None = None
            failure: str | None = None
            if chart.going_public and chart.chart_hash is not None:
                try:
                    seconds = chart.chart_seconds
                    if seconds is None:
                        seconds = measured = await self.chart_seconds(chart, chart.chart_hash)
                    if seconds < self.constants.charts.min_public_chart_seconds:
                        return Verdict(chart, REASON_CHART, seconds, measured)
                except CheckFailure as error:
                    failure = error.reason
            if chart.music_hash is not None:
                try:
                    seconds = await self.song_seconds(chart, chart.music_hash)
                    if seconds < self.constants.audio.min_music_seconds:
                        return Verdict(chart, REASON_SONG, seconds, measured)
                except CheckFailure as error:
                    failure = failure or error.reason
            return Verdict(chart, measured=measured, failure=failure)


def format_seconds(seconds: float) -> str:
    """Format a length as minutes and seconds.

    Args:
        seconds: Length in seconds.

    Returns:
        str: Text like ``0:12.40``.
    """
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:05.2f}"


async def load_charts(database: Database) -> list[Chart]:
    """Read every chart that isn't deleted.

    Args:
        database: Open database pool.

    Returns:
        list[Chart]: The charts.
    """
    async with database.connection() as connection:
        rows = await connection.fetch(CHARTS_QUERY)
    return [
        Chart(
            chart_id=row["id"],
            owner=row["author"],
            status=row["status"],
            title=row["title"],
            music_hash=row["music_file_hash"],
            chart_hash=row["chart_file_hash"],
            chart_seconds=row["chart_seconds"],
            scheduled=row["scheduled"],
        )
        for row in rows
    ]


async def scan_charts(scan: Scan, charts: list[Chart]) -> list[Verdict]:
    """Check every chart, printing progress.

    Args:
        scan: The scan.
        charts: Charts to check.

    Returns:
        list[Verdict]: One verdict per chart.
    """
    verdicts: list[Verdict] = []
    last = time.monotonic()
    for task in asyncio.as_completed([scan.check(chart) for chart in charts]):
        verdicts.append(await task)
        if time.monotonic() - last >= PROGRESS_SECONDS:
            last = time.monotonic()
            print(f"  checked {len(verdicts)}/{len(charts)}", flush=True)
    return verdicts


def report(verdicts: list[Verdict], constants: Constants) -> list[Verdict]:
    """Print what the scan found.

    Args:
        verdicts: Scan results.
        constants: Validated constants.yml.

    Returns:
        list[Verdict]: The charts to delete, songs first.
    """
    songs = sorted((v for v in verdicts if v.reason == REASON_SONG), key=lambda v: v.chart.chart_id)
    short = sorted(
        (v for v in verdicts if v.reason == REASON_CHART),
        key=lambda v: v.chart.chart_id,
    )
    failures = Counter(v.failure for v in verdicts if v.failure is not None)
    public = sum(1 for v in verdicts if v.chart.status == "PUBLIC")
    scheduled = sum(1 for v in verdicts if v.chart.status != "PUBLIC" and v.chart.scheduled)
    print(f"\nChecked {len(verdicts)} chart(s), {public} public, {scheduled} scheduled.")
    print(f"\nSongs under {constants.audio.min_music_seconds:g} seconds: {len(songs)}")
    for verdict in songs:
        print(
            f"  {verdict.chart.chart_id}  {verdict.chart.status:<8}  "
            f"song {format_seconds(verdict.seconds or 0)}  {verdict.chart.title!r}"
        )
    minimum = constants.charts.min_public_chart_seconds
    print(f"\nPublic or scheduled charts with under {minimum:g} seconds charted: {len(short)}")
    for verdict in short:
        state = "PUBLIC" if verdict.chart.status == "PUBLIC" else "SCHEDULED"
        print(
            f"  {verdict.chart.chart_id}  {state:<9}  "
            f"chart {format_seconds(verdict.seconds or 0)}  {verdict.chart.title!r}"
        )
    if failures:
        print(f"\nCouldn't check {sum(failures.values())} chart(s), they are kept:")
        for reason, count in failures.most_common():
            print(f"  {reason}: {count}")
        for verdict in sorted(verdicts, key=lambda v: v.chart.chart_id):
            if verdict.failure is not None:
                print(f"    {verdict.chart.chart_id}  {verdict.failure}")
    return songs + short


async def delete(database: Database, targets: list[Verdict]) -> tuple[int, int]:
    """Soft-delete charts and log each as an admin key action, without Discord posts.

    Args:
        database: Open database pool.
        targets: Charts to delete.

    Returns:
        tuple[int, int]: Charts deleted, and charts skipped because they changed meanwhile.
    """
    deleted = skipped = 0
    for verdict in targets:
        chart = verdict.chart
        async with database.transaction() as connection:
            row = await fetch_value(
                connection,
                DELETE_QUERY,
                chart.chart_id,
                chart.music_hash,
                chart.chart_hash,
                verdict.reason == REASON_SONG,
            )
            if row is None:
                skipped += 1
                continue
            await staff_actions_db.log_action(
                connection,
                None,
                True,
                "delete",
                "chart",
                chart.chart_id,
                None,
                "soft_deleted",
            )
        deleted += 1
    return deleted, skipped


async def store_measured(database: Database, verdicts: list[Verdict]) -> int:
    """Store chart lengths measured for charts that are kept.

    Args:
        database: Open database pool.
        verdicts: Scan results.

    Returns:
        int: Charts given a measured length.
    """
    rows = [
        (v.chart.chart_id, v.measured, v.chart.chart_hash)
        for v in verdicts
        if v.measured is not None and v.reason is None and v.chart.chart_hash is not None
    ]
    if rows:
        async with database.connection() as connection:
            await connection.executemany(STORE_SECONDS_QUERY, rows)
    return len(rows)


async def run(args: argparse.Namespace) -> None:
    """Find charts breaking the length rules and optionally delete them.

    Args:
        args: Parsed command line.
    """
    config: Config = read_config(args.config)
    constants: Constants = read_constants(args.constants)
    database = Database()
    await database.open(small_pool(config.database))
    storage = Storage(config.storage)
    await storage.open()
    scan = Scan(
        storage,
        constants,
        config.charts.max_decompressed_size,
        config.charts.conversion_memory_limit,
        args.concurrency,
    )
    try:
        charts = await load_charts(database)
        print(f"Checking {len(charts)} chart(s)...")
        verdicts = await scan_charts(scan, charts)
        targets = report(verdicts, constants)
        if not args.apply:
            print("\nDry run, nothing was deleted. Re-run with --apply to delete them.")
            return
        if targets and (
            args.yes
            or confirm(
                f"\nDelete {len(targets)} chart(s)? Undo with rollback_staff_actions --admin-key "
                "until they are purged. Type 'delete' to continue:",
                "delete",
            )
        ):
            deleted, skipped = await delete(database, targets)
            print(f"Deleted {deleted} chart(s), skipped {skipped} that changed meanwhile.")
        elif targets:
            print("Aborted, nothing was deleted.")
        stored = await store_measured(database, verdicts)
        if stored:
            print(f"Stored the measured length of {stored} chart(s).")
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as error:
        raise SystemExit(f"database error: {error!r}") from None
    finally:
        scan.close()
        await storage.close()
        await database.close()


def main() -> None:
    """Parse the command line and run the scan."""
    parser = argparse.ArgumentParser(
        description=(
            "Soft-delete charts whose song is shorter than audio.min_music_seconds (any "
            "visibility) and public or scheduled charts with less than "
            "charts.min_public_chart_seconds charted. Dry run by default."
        )
    )
    add_config_argument(parser)
    add_constants_argument(parser)
    parser.add_argument("--apply", action="store_true", help="delete the charts found")
    parser.add_argument(
        "--yes", action="store_true", help="with --apply, skip the confirmation prompt"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"charts checked at once (default: {DEFAULT_CONCURRENCY})",
    )
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
