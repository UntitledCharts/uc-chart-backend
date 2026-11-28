import argparse
import asyncio
import contextlib
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, cast

import aioboto3
import aiohttp
import asyncpg
from aiobotocore.config import AioConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.database import charts as charts_db
from app.database.core import Database
from app.services.storage import Storage, StorageError
from app.settings import Config, StorageConfig
from scripts._common import (
    add_config_argument,
    connect,
    format_bytes,
    read_config,
    small_pool,
)

PREVIEW_SECONDS = 30
CONTENT_TYPE = "audio/mpeg"
MAX_MP3_BYTES_PER_SECOND = 320_000 // 8
AUDIO_WINDOW = (PREVIEW_SECONDS + 1) * MAX_MP3_BYTES_PER_SECOND + 64 * 1024
FIRST_RANGE = AUDIO_WINDOW + 256 * 1024
TAIL_MARGIN = 64 * 1024
FFMPEG_TIMEOUT_SECONDS = 60
DB_POOL_SIZE = 8
DEFAULT_CONCURRENCY = 32
DEFAULT_BATCH_SIZE = 500
DEFAULT_SHOW = 20
STATUS_WIDTH = 78
MISSING_CODES = frozenset({"NoSuchKey", "NotFound", "404"})

TARGETS = """
    FROM charts c
    JOIN accounts a ON a.sonolus_id = c.author
    WHERE c.preview_file_hash IS NULL
      AND c.deleted_at IS NULL
      AND a.deleted_at IS NULL
      AND (c.status = 'PUBLIC' OR c.scheduled_publish IS NOT NULL)
"""
COUNT_QUERY = (
    f"SELECT count(*) AS total, count(*) FILTER (WHERE c.status = 'PUBLIC') AS public {TARGETS}"
)
PAGE_QUERY = (
    f"SELECT c.id, c.author, c.music_file_hash {TARGETS} AND c.id > $1 ORDER BY c.id LIMIT $2"
)
UPDATE_QUERY = """
    UPDATE charts SET preview_file_hash = $2
    WHERE id = $1 AND preview_file_hash IS NULL AND music_file_hash = $3
    RETURNING id
"""
FILES_QUERY = f"SELECT {', '.join(charts_db.FILE_COLUMNS)} FROM charts WHERE id = $1"

FFMPEG_ARGUMENTS = (
    "-nostdin",
    "-hide_banner",
    "-loglevel",
    "error",
    "-protocol_whitelist",
    "pipe,file",
    "-f",
    "mp3",
    "-i",
    "pipe:0",
    "-map",
    "0:a:0",
    "-c",
    "copy",
    "-t",
    str(PREVIEW_SECONDS),
    "-map_metadata",
    "-1",
    "-map_chapters",
    "-1",
    "-id3v2_version",
    "0",
    "-f",
    "mp3",
    "-y",
)
CHILD_OPTIONS: dict[str, Any] = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    if sys.platform == "win32"
    else {"start_new_session": True}
)
STORAGE_ERRORS = (
    ClientError,
    BotoCoreError,
    aiohttp.ClientError,
    StorageError,
    OSError,
)
DATABASE_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError)


class ChartFailure(Exception):
    """Raised when one chart's preview can't be generated.

    Attributes:
        reason: Failure category shown in the summary.
        detail: What went wrong.
    """

    def __init__(self, reason: str, detail: str) -> None:
        """Create a failure.

        Args:
            reason: Failure category shown in the summary.
            detail: What went wrong.
        """
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Target:
    """A chart that needs a preview.

    Attributes:
        chart_id: Chart id.
        owner: Owner's Sonolus id, the first segment of the chart's storage keys.
        music_hash: SHA-1 of the music file.
    """

    chart_id: str
    owner: str
    music_hash: str


@dataclass
class Stats:
    """Counters of an --apply run.

    Attributes:
        total: Charts expected to be processed.
        done: Charts finished, whatever the outcome.
        generated: Previews stored and referenced.
        skipped: Charts whose row changed meanwhile, by reason.
        failed: Charts that failed, by reason.
        downloaded: Bytes downloaded.
        music_bytes: Total size of the music files read.
        refetched: Music files downloaded in full because their start wasn't enough.
        uploaded: Bytes uploaded.
        started: Monotonic start time.
    """

    total: int
    done: int = 0
    generated: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    failed: Counter[str] = field(default_factory=Counter)
    downloaded: int = 0
    music_bytes: int = 0
    refetched: int = 0
    uploaded: int = 0
    started: float = field(default_factory=time.monotonic)


class PreviewStorage(Storage):
    """Storage client with a connection pool sized for the run, and ranged downloads.

    Attributes:
        config: Storage settings.
        connections: Largest number of open HTTP connections.
    """

    def __init__(self, config: StorageConfig, connections: int) -> None:
        """Create an unopened storage client.

        Args:
            config: Storage settings.
            connections: Largest number of open HTTP connections.
        """
        super().__init__(config)
        self.connections = connections

    async def open(self) -> None:
        """Open the S3 client with room for every concurrent request and standard retries."""
        session = aioboto3.Session(
            aws_access_key_id=self.config.access_key_id,
            aws_secret_access_key=self.config.secret_access_key,
            region_name=self.config.region,
        )
        client_context = cast(
            Any,
            session.client(
                "s3",
                endpoint_url=self.config.endpoint,
                config=AioConfig(
                    max_pool_connections=self.connections,
                    retries={"mode": "standard", "max_attempts": 5},
                ),
            ),
        )
        self._client = await self._stack.enter_async_context(client_context)

    async def read_range(self, key: str, start: int, end: int) -> tuple[bytes, int]:
        """Download part of an object.

        Args:
            key: Object key.
            start: Offset of the first byte.
            end: Offset after the last byte.

        Returns:
            tuple[bytes, int]: The bytes, and the object's total size.
        """
        response = await self._client.get_object(
            Bucket=self.config.bucket, Key=key, Range=f"bytes={start}-{end - 1}"
        )
        async with response["Body"] as body:
            data = await body.read()
        total = str(response.get("ContentRange", "")).rpartition("/")[2]
        return data, int(total) if total.isdigit() else start + len(data)


@dataclass
class Run:
    """Shared state of an --apply run.

    Attributes:
        database: Open database pool.
        storage: Open storage client.
        executor: Threads that each drive one ffmpeg process at a time.
        ffmpeg: Path of the ffmpeg program.
        workdir: Folder for ffmpeg's output files.
        stats: Counters.
        stop: Set once the operator asked to stop.
    """

    database: Database
    storage: PreviewStorage
    executor: ThreadPoolExecutor
    ffmpeg: str
    workdir: str
    stats: Stats
    stop: asyncio.Event = field(default_factory=asyncio.Event)


def status(message: str, final: bool = False) -> None:
    """Rewrite the progress line.

    Args:
        message: Text to show.
        final: End the line instead of leaving it for the next update.
    """
    if sys.stdout.isatty():
        sys.stdout.write("\r" + message.ljust(STATUS_WIDTH) + ("\n" if final else ""))
    else:
        sys.stdout.write(message + "\n")
    sys.stdout.flush()


def log(message: str) -> None:
    """Print a line above the progress line.

    Args:
        message: Text to print.
    """
    if sys.stdout.isatty():
        sys.stdout.write("\r" + " " * STATUS_WIDTH + "\r")
    print(message, flush=True)


def format_duration(seconds: float) -> str:
    """Format a duration as hours, minutes and seconds.

    Args:
        seconds: Duration in seconds.

    Returns:
        str: Text like ``1:02:03``.
    """
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}"


def progress_text(stats: Stats) -> str:
    """Describe how far a run is.

    Args:
        stats: Counters.

    Returns:
        str: One progress line.
    """
    rate = stats.done / max(time.monotonic() - stats.started, 1e-6)
    left = max(stats.total - stats.done, 0)
    eta = format_duration(left / rate) if rate > 0 else "?"
    return (
        f"  {stats.done}/{stats.total} done  {stats.generated} ok  "
        f"{sum(stats.failed.values())} failed  {sum(stats.skipped.values())} skipped  "
        f"{rate:.1f}/s  ETA {eta}"
    )


def audio_offset(data: bytes) -> int:
    """Find where the audio starts after the ID3v2 tags at the start of an MP3.

    Args:
        data: Start of the file.

    Returns:
        int: Offset of the first byte after the tags, possibly beyond ``data``.
    """
    offset = 0
    while len(data) >= offset + 10 and data[offset : offset + 3] == b"ID3":
        size = 0
        for byte in data[offset + 6 : offset + 10]:
            size = (size << 7) | (byte & 0x7F)
        footer = 10 if data[offset + 5] & 0x10 else 0
        offset += 10 + size + footer
    return offset


def last_line(output: bytes) -> str:
    """Pick the last non-empty line of a program's error output.

    Args:
        output: Raw error output.

    Returns:
        str: The line, shortened, or an empty string.
    """
    lines = [line.strip() for line in output.decode("utf-8", "replace").splitlines()]
    text = next((line for line in reversed(lines) if line), "")
    return text if len(text) <= 200 else text[:197] + "..."


def cut(ffmpeg: str, music: bytes, workdir: str) -> tuple[bytes, str]:
    """Copy the first seconds of the first audio stream into a bare MP3, without re-encoding.

    Args:
        ffmpeg: Path of the ffmpeg program.
        music: Start or whole of the music file.
        workdir: Folder for ffmpeg's output file.

    Returns:
        tuple[bytes, str]: The preview and its SHA-1 hex digest.
    """
    handle, output = tempfile.mkstemp(suffix=".mp3", dir=workdir)
    os.close(handle)
    try:
        result = subprocess.run(
            [ffmpeg, *FFMPEG_ARGUMENTS, output],
            input=music,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
            **CHILD_OPTIONS,
        )
        if result.returncode != 0:
            detail = last_line(result.stderr) or f"exit code {result.returncode}"
            raise ChartFailure("ffmpeg failed", detail)
        with open(output, "rb") as file:
            preview = file.read()
    except subprocess.TimeoutExpired:
        raise ChartFailure("ffmpeg failed", f"timed out after {FFMPEG_TIMEOUT_SECONDS}s") from None
    except OSError as error:
        raise ChartFailure("ffmpeg failed", repr(error)) from None
    finally:
        with contextlib.suppress(OSError):
            os.remove(output)
    if not preview:
        raise ChartFailure("ffmpeg failed", "the output holds no audio")
    return preview, hashlib.sha1(preview).hexdigest()


async def fetch(run: Run, key: str, start: int, end: int) -> tuple[bytes, int]:
    """Download part of a music file, turning storage errors into chart failures.

    Args:
        run: Run state.
        key: Object key.
        start: Offset of the first byte.
        end: Offset after the last byte.

    Returns:
        tuple[bytes, int]: The bytes, and the file's total size.
    """
    try:
        data, total = await run.storage.read_range(key, start, end)
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in MISSING_CODES:
            raise ChartFailure("music file missing", key) from None
        if code == "InvalidRange":
            raise ChartFailure("music file empty", key) from None
        raise ChartFailure("download failed", f"{key}: {error}") from None
    except STORAGE_ERRORS as error:
        raise ChartFailure("download failed", f"{key}: {error!r}") from None
    if not data:
        if total == 0:
            raise ChartFailure("music file empty", key)
        raise ChartFailure("download failed", f"{key}: storage sent no bytes")
    run.stats.downloaded += len(data)
    return data, total


async def download_head(run: Run, key: str) -> tuple[bytes, int]:
    """Download a music file's tags plus enough audio for the preview, or all of a small file.

    Args:
        run: Run state.
        key: Object key.

    Returns:
        tuple[bytes, int]: The start of the file, and its total size.
    """
    music, total = await fetch(run, key, 0, FIRST_RANGE)
    run.stats.music_bytes += total
    while len(music) < total:
        needed = min(total, audio_offset(music) + AUDIO_WINDOW)
        if needed <= len(music):
            break
        more, total = await fetch(run, key, len(music), needed)
        music += more
    return music, total


async def make_preview(run: Run, target: Target) -> tuple[bytes, str]:
    """Download the start of a chart's music and cut the preview from it.

    Args:
        run: Run state.
        target: The chart.

    Returns:
        tuple[bytes, str]: The preview and its SHA-1 hex digest.
    """
    key = f"{target.owner}/{target.chart_id}/{target.music_hash}"
    loop = asyncio.get_running_loop()
    music, total = await download_head(run, key)
    preview, digest = await loop.run_in_executor(run.executor, cut, run.ffmpeg, music, run.workdir)
    if len(music) < total and len(music) - audio_offset(music) - len(preview) < TAIL_MARGIN:
        rest, _ = await fetch(run, key, len(music), total)
        music += rest
        run.stats.refetched += 1
        preview, digest = await loop.run_in_executor(
            run.executor, cut, run.ffmpeg, music, run.workdir
        )
    return preview, digest


async def store_preview(run: Run, target: Target, preview: bytes, digest: str) -> str | None:
    """Upload a preview and reference it, cleaning up when the chart changed meanwhile.

    Args:
        run: Run state.
        target: The chart.
        preview: Preview file.
        digest: SHA-1 hex digest of the preview.

    Returns:
        str | None: Why the chart was skipped, or None when the preview was referenced.
    """
    key = f"{target.owner}/{target.chart_id}/{digest}"
    try:
        await run.storage.put(key, preview, CONTENT_TYPE)
    except STORAGE_ERRORS as error:
        raise ChartFailure("upload failed", f"{key}: {error!r}") from None
    run.stats.uploaded += len(preview)
    try:
        async with run.database.connection() as connection:
            updated = await connection.fetchval(
                UPDATE_QUERY, target.chart_id, digest, target.music_hash
            )
            if updated is not None:
                return None
            row = await connection.fetchrow(FILES_QUERY, target.chart_id)
    except DATABASE_ERRORS as error:
        raise ChartFailure(
            "database update failed", f"{error!r}, uploaded {key} was kept"
        ) from None
    if row is None:
        reason = "chart purged meanwhile"
    elif row["preview_file_hash"] is not None:
        reason = "preview added meanwhile"
    else:
        reason = "music replaced meanwhile"
    if row is None or digest not in {row[column] for column in charts_db.FILE_COLUMNS}:
        try:
            await run.storage.delete_keys([key])
        except STORAGE_ERRORS as error:
            raise ChartFailure("cleanup failed", f"{reason}, {key} was kept: {error!r}") from None
    return reason


async def handle(run: Run, target: Target) -> None:
    """Generate, store and reference one chart's preview, recording the outcome.

    Args:
        run: Run state.
        target: The chart.
    """
    try:
        preview, digest = await make_preview(run, target)
        skipped = await store_preview(run, target, preview, digest)
    except ChartFailure as failure:
        run.stats.failed[failure.reason] += 1
        log(f"[FAILED] {target.chart_id}: {failure}")
    else:
        if skipped is None:
            run.stats.generated += 1
        else:
            run.stats.skipped[skipped] += 1
            log(f"[SKIPPED] {target.chart_id}: {skipped}")
    run.stats.done += 1


async def work(run: Run, queue: asyncio.Queue[Target | None]) -> None:
    """Process charts from the queue until the end marker, draining it once stopping.

    Args:
        run: Run state.
        queue: Charts to process, ended by None.
    """
    while (target := await queue.get()) is not None:
        if not run.stop.is_set():
            await handle(run, target)


async def produce(
    run: Run,
    queue: asyncio.Queue[Target | None],
    batch_size: int,
    limit: int | None,
    workers: int,
) -> None:
    """Page through the charts that need a preview by id and queue them.

    Args:
        run: Run state.
        queue: Queue the workers read.
        batch_size: Rows per page.
        limit: Largest number of charts to queue, None for all.
        workers: Number of workers, each getting an end marker.
    """
    last_id = ""
    remaining = limit
    while not run.stop.is_set() and (remaining is None or remaining > 0):
        size = batch_size if remaining is None else min(batch_size, remaining)
        try:
            async with run.database.connection() as connection:
                rows = await connection.fetch(PAGE_QUERY, last_id, size)
        except DATABASE_ERRORS as error:
            log(f"[ERROR] reading charts after {last_id!r} failed, stopping: {error!r}")
            run.stop.set()
            break
        if not rows:
            break
        for row in rows:
            if run.stop.is_set():
                break
            await queue.put(Target(row["id"], row["author"], row["music_file_hash"]))
        last_id = rows[-1]["id"]
        if remaining is not None:
            remaining -= len(rows)
    for _ in range(workers):
        await queue.put(None)


async def report(stats: Stats) -> None:
    """Show the progress line every second, or every ten seconds when not on a terminal.

    Args:
        stats: Counters.
    """
    interval = 1.0 if sys.stdout.isatty() else 10.0
    while True:
        await asyncio.sleep(interval)
        status(progress_text(stats))


def install_interrupt_handler(run: Run, task: asyncio.Task[Any]) -> Callable[[], None]:
    """Make the first Ctrl+C finish the charts in progress and the second one abort.

    Args:
        run: Run state.
        task: Task to cancel on the second interrupt.

    Returns:
        Callable[[], None]: Function putting the previous handlers back.
    """
    loop = asyncio.get_running_loop()
    numbers = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32":
        numbers.append(signal.SIGBREAK)
    previous = {number: signal.getsignal(number) for number in numbers}

    def interrupt() -> None:
        """Stop taking new charts, or abort when already stopping."""
        if run.stop.is_set():
            log("Aborting, charts in progress are dropped.")
            task.cancel()
            return
        run.stop.set()
        log("Stopping after the charts in progress, press Ctrl+C again to abort.")

    def on_signal(_number: int, _frame: FrameType | None) -> None:
        """Hand a termination signal over to the event loop.

        Args:
            _number: Signal number.
            _frame: Interrupted frame.
        """
        loop.call_soon_threadsafe(interrupt)

    for number in numbers:
        signal.signal(number, on_signal)

    def restore() -> None:
        """Put the previous signal handlers back."""
        for number, handler in previous.items():
            if handler is not None:
                signal.signal(number, handler)

    return restore


def print_summary(stats: Stats, interrupted: bool) -> None:
    """Print the outcome of an --apply run.

    Args:
        stats: Counters.
        interrupted: Whether the run stopped before the end.
    """
    elapsed = max(time.monotonic() - stats.started, 1e-6)
    print("\nSummary:")
    print(f"  generated {stats.generated:>10}")
    for title, counts in (("failed", stats.failed), ("skipped", stats.skipped)):
        print(f"  {title:<9} {sum(counts.values()):>10}")
        for reason, count in sorted(counts.items()):
            print(f"    {reason:<28} {count:>6}")
    if interrupted:
        print(f"  not done  {max(stats.total - stats.done, 0):>10}")
    share = f" of {format_bytes(stats.music_bytes)} music" if stats.music_bytes else ""
    print(
        f"  downloaded {format_bytes(stats.downloaded)}{share}, "
        f"{stats.refetched} file(s) in full after a short start"
    )
    print(f"  uploaded   {format_bytes(stats.uploaded)}")
    print(
        f"  elapsed    {format_duration(elapsed)} ({elapsed:.1f}s), "
        f"{stats.done / elapsed:.1f} charts/s",
        flush=True,
    )


async def dry_run(config: Config, args: argparse.Namespace) -> None:
    """Count and list the charts that need a preview, without changing anything.

    Args:
        config: Validated config.yml.
        args: Parsed command line.
    """
    connection = await connect(config.database)
    try:
        counts = await connection.fetchrow(COUNT_QUERY)
        sample = await connection.fetch(PAGE_QUERY, "", args.show) if args.show else []
    finally:
        await connection.close()
    assert counts is not None
    total, public = counts["total"], counts["public"]
    print(
        f"{total} chart(s) need a preview: {public} public, "
        f"{total - public} scheduled to go public."
    )
    if args.limit is not None and args.limit < total:
        print(f"With --limit {args.limit}, --apply processes the first {args.limit} by id.")
    if sample:
        print(f"First {len(sample)} by id:")
        for row in sample:
            print(f"  {row['id']}")
    if shutil.which("ffmpeg") is None:
        print("WARNING: ffmpeg is not on PATH, --apply needs it.")
    if total:
        print("\nDry run, nothing was changed. Re-run with --apply to generate them.")


async def process_all(run: Run, args: argparse.Namespace, task: asyncio.Task[Any]) -> bool:
    """Run the producer and the workers until every chart is done or the operator stops them.

    Args:
        run: Run state.
        args: Parsed command line.
        task: The task running this function, cancelled by a second interrupt.

    Returns:
        bool: Whether a second interrupt aborted the charts in progress.
    """
    queue: asyncio.Queue[Target | None] = asyncio.Queue(maxsize=args.concurrency * 2)
    restore = install_interrupt_handler(run, task)
    reporter = asyncio.create_task(report(run.stats))
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(produce(run, queue, args.batch_size, args.limit, args.concurrency))
            for _ in range(args.concurrency):
                group.create_task(work(run, queue))
    except asyncio.CancelledError:
        task.uncancel()
        return True
    finally:
        restore()
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)
    return False


async def apply(config: Config, args: argparse.Namespace) -> None:
    """Generate, upload and reference the missing previews.

    Args:
        config: Validated config.yml.
        args: Parsed command line.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg not found on PATH")
    task = asyncio.current_task()
    assert task is not None
    database = Database()
    storage = PreviewStorage(config.storage, args.concurrency + 4)
    try:
        await database.open(small_pool(config.database, DB_POOL_SIZE))
        await storage.open()
        async with database.connection() as connection:
            counts = await connection.fetchrow(COUNT_QUERY)
        assert counts is not None
        total = counts["total"] if args.limit is None else min(counts["total"], args.limit)
        if total == 0:
            print("No chart needs a preview.")
            return
        print(
            f"Generating {total} preview(s) with {args.workers} ffmpeg worker(s) and "
            f"{args.concurrency} chart(s) in flight..."
        )
        with tempfile.TemporaryDirectory(prefix="uc-previews-", ignore_cleanup_errors=True) as path:
            executor = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="ffmpeg")
            run = Run(database, storage, executor, ffmpeg, path, Stats(total))
            try:
                aborted = await process_all(run, args, task)
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
            interrupted = aborted or run.stop.is_set()
            status(progress_text(run.stats), final=True)
            print_summary(run.stats, interrupted)
            failed = sum(run.stats.failed.values())
            if interrupted:
                raise SystemExit(
                    f"{'aborted' if aborted else 'stopped'} early, "
                    "re-run to process the remaining charts"
                )
            if failed:
                raise SystemExit(
                    f"{failed} chart(s) failed, see [FAILED] above; a re-run retries them"
                )
    finally:
        await storage.close()
        await database.close()


async def run_script(args: argparse.Namespace) -> None:
    """Load the config and run the dry run or the generation.

    Args:
        args: Parsed command line.
    """
    config = read_config(args.config)
    print(f"Database {config.database.database}, bucket {config.storage.bucket}")
    if args.apply:
        await apply(config, args)
    else:
        await dry_run(config, args)


def main() -> None:
    """Parse the command line and generate the missing previews."""
    parser = argparse.ArgumentParser(
        description=(
            f"Cut a {PREVIEW_SECONDS} second preview, without re-encoding, from the music of every "
            "public or scheduled chart that has none, upload it and reference it. Skips deleted "
            "charts and charts of accounts pending deletion. Dry run by default."
        )
    )
    add_config_argument(parser)
    parser.add_argument("--apply", action="store_true", help="generate and store the previews")
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="ffmpeg processes at once (default: CPU count)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"charts downloading, uploading or updating at once (default: {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"charts read from the database per page (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="process at most this many charts, the first ones by id",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=DEFAULT_SHOW,
        help=f"chart ids the dry run lists (default: {DEFAULT_SHOW})",
    )
    args = parser.parse_args()
    for name in ("workers", "concurrency", "batch_size", "limit"):
        value = getattr(args, name)
        if value is not None and value < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    if args.show < 0:
        parser.error("--show must not be negative")
    asyncio.run(run_script(args))


if __name__ == "__main__":
    main()
