import argparse
import asyncio
import os
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass

import aiohttp
import asyncpg
from botocore.exceptions import BotoCoreError, ClientError

from app.database import charts as charts_db
from app.database.core import Database
from app.services import conversion
from app.services.storage import Storage, StorageError
from app.settings import Config
from scripts._common import add_config_argument, read_config, small_pool

DEFAULT_CONCURRENCY = 2
PROGRESS_SECONDS = 5.0
SHOWN_FAILURES = 20
STORAGE_ERRORS = (
    ClientError,
    BotoCoreError,
    aiohttp.ClientError,
    StorageError,
    OSError,
)

PENDING_QUERY = """
    SELECT id, author, chart_file_hash
    FROM charts
    WHERE chart_seconds IS NULL AND chart_file_hash IS NOT NULL
    ORDER BY id
"""


class MeasureFailure(Exception):
    """A chart whose length couldn't be measured.

    Attributes:
        reason: Short reason, used to group failures.
        detail: What went wrong.
    """

    def __init__(self, reason: str, detail: str) -> None:
        """Create a failure.

        Args:
            reason: Short reason, used to group failures.
            detail: What went wrong.
        """
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PendingChart:
    """A chart with no stored length.

    Attributes:
        chart_id: Chart id.
        owner: Owner's Sonolus id.
        chart_hash: Level data hash.
    """

    chart_id: str
    owner: str
    chart_hash: str


class Meter:
    """Measures charts in capped child processes, a few at a time.

    Attributes:
        storage: Open storage client.
        limit: Largest decompressed level data size accepted.
        memory_limit: Largest memory one measuring process may use, in bytes.
        slots: Charts measured at once.
        concurrency: Measuring processes.
        processes: Processes reading level data.
    """

    def __init__(self, storage: Storage, limit: int, memory_limit: int, concurrency: int) -> None:
        """Create a meter.

        Args:
            storage: Open storage client.
            limit: Largest decompressed level data size accepted.
            memory_limit: Largest memory one measuring process may use, in bytes.
            concurrency: Charts measured at once.
        """
        self.storage = storage
        self.limit = limit
        self.memory_limit = memory_limit
        self.slots = asyncio.Semaphore(concurrency)
        self.concurrency = concurrency
        self.processes = self._pool()

    def _pool(self) -> ProcessPoolExecutor:
        """Start measuring processes capped like the API's conversion children.

        Returns:
            ProcessPoolExecutor: The pool.
        """
        return ProcessPoolExecutor(
            max_workers=self.concurrency,
            initializer=conversion.confine,
            initargs=(self.memory_limit, os.getpid()),
        )

    def close(self) -> None:
        """Stop the measuring processes."""
        self.processes.shutdown(wait=False, cancel_futures=True)

    async def measure(self, chart: PendingChart) -> float:
        """Download a chart's level data and read when its last note plays.

        Args:
            chart: The chart.

        Returns:
            float: Time of the last note in seconds.
        """
        async with self.slots:
            key = f"{chart.owner}/{chart.chart_id}/{chart.chart_hash}"
            try:
                stored = await self.storage.get(key)
            except STORAGE_ERRORS as error:
                raise MeasureFailure("download failed", f"{key}: {error!r}") from None
            if stored is None:
                raise MeasureFailure("file missing", key)
            pool = self.processes
            try:
                return await asyncio.get_running_loop().run_in_executor(
                    pool, conversion.level_data_seconds, stored[0], self.limit
                )
            except BrokenProcessPool:
                if self.processes is pool:
                    self.processes = self._pool()
                    pool.shutdown(wait=False, cancel_futures=True)
                raise MeasureFailure(
                    "chart unreadable", "the measuring process crashed or ran out of memory"
                ) from None
            except Exception as error:
                raise MeasureFailure("chart unreadable", repr(error)) from None


async def load_pending(database: Database) -> list[PendingChart]:
    """Read every chart that has no stored length.

    Args:
        database: Open database pool.

    Returns:
        list[PendingChart]: The charts.
    """
    async with database.connection() as connection:
        rows = await connection.fetch(PENDING_QUERY)
    return [
        PendingChart(chart_id=row["id"], owner=row["author"], chart_hash=row["chart_file_hash"])
        for row in rows
    ]


async def backfill(
    database: Database, meter: Meter, charts: list[PendingChart]
) -> tuple[int, list[tuple[PendingChart, MeasureFailure]]]:
    """Measure each chart and store its length as soon as it is known.

    Args:
        database: Open database pool.
        meter: The meter.
        charts: Charts to measure.

    Returns:
        tuple[int, list[tuple[PendingChart, MeasureFailure]]]: Charts stored, and the ones
            that couldn't be measured.
    """

    async def one(chart: PendingChart) -> tuple[PendingChart, float | MeasureFailure]:
        """Measure one chart.

        Args:
            chart: The chart.

        Returns:
            tuple[PendingChart, float | MeasureFailure]: The chart and its length or failure.
        """
        try:
            return chart, await meter.measure(chart)
        except MeasureFailure as error:
            return chart, error

    stored = 0
    failures: list[tuple[PendingChart, MeasureFailure]] = []
    last = time.monotonic()
    for done, task in enumerate(asyncio.as_completed([one(chart) for chart in charts]), 1):
        chart, result = await task
        if isinstance(result, MeasureFailure):
            failures.append((chart, result))
        else:
            async with database.connection() as connection:
                await charts_db.set_chart_seconds(
                    connection, chart.chart_id, result, chart.chart_hash
                )
            stored += 1
        if time.monotonic() - last >= PROGRESS_SECONDS:
            last = time.monotonic()
            print(f"  measured {done}/{len(charts)}", flush=True)
    return stored, failures


def report(failures: list[tuple[PendingChart, MeasureFailure]]) -> None:
    """Print the charts that couldn't be measured, grouped by reason.

    Args:
        failures: Charts and why they failed.
    """
    if not failures:
        return
    counts = Counter(failure.reason for _, failure in failures)
    print(f"\nCouldn't measure {len(failures)} chart(s); they are measured when they go public:")
    for reason, count in counts.most_common():
        print(f"  {reason}: {count}")
    for chart, failure in failures[:SHOWN_FAILURES]:
        print(f"  {chart.chart_id}: {failure}")
    if len(failures) > SHOWN_FAILURES:
        print(f"  ...and {len(failures) - SHOWN_FAILURES} more")


async def run(args: argparse.Namespace) -> None:
    """Find charts without a stored length and optionally measure them.

    Args:
        args: Parsed command line.
    """
    config: Config = read_config(args.config)
    database = Database()
    await database.open(small_pool(config.database))
    storage = Storage(config.storage)
    await storage.open()
    meter = Meter(
        storage,
        config.charts.max_decompressed_size,
        config.charts.conversion_memory_limit,
        args.concurrency,
    )
    try:
        charts = await load_pending(database)
        print(f"{len(charts)} chart(s) have no stored length.")
        if not args.apply:
            print("Dry run, nothing was measured. Re-run with --apply to measure and store them.")
            return
        if not charts:
            return
        stored, failures = await backfill(database, meter, charts)
        print(f"Stored the length of {stored} chart(s).")
        report(failures)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as error:
        raise SystemExit(f"database error: {error!r}") from None
    finally:
        meter.close()
        await storage.close()
        await database.close()


def main() -> None:
    """Parse the command line and run the backfill."""
    parser = argparse.ArgumentParser(
        description=(
            "Measure and store the length of charts uploaded before lengths were stored, so "
            "making them public doesn't have to measure them first. Safe to run while the API "
            "is up and to run again. Dry run by default."
        )
    )
    add_config_argument(parser)
    parser.add_argument("--apply", action="store_true", help="measure and store the lengths")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"charts measured at once (default: {DEFAULT_CONCURRENCY})",
    )
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
