import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import charts as charts_db
from app.database.core import Database
from app.services.storage import DELETE_BATCH_SIZE, Storage
from app.settings import Config
from scripts._common import (
    add_config_argument,
    confirm,
    format_bytes,
    read_config,
    small_pool,
)

LIST_CONCURRENCY = 16
PROGRESS_EVERY = 25
UNRECOGNIZED = "unrecognized"
STALE_LIMIT = timedelta(hours=24)
MISSING_LIMIT = 0.01


@dataclass(frozen=True)
class StoredObject:
    """An object found in the bucket.

    Attributes:
        key: Object key.
        size: Size in bytes.
        last_modified: Last write time.
    """

    key: str
    size: int
    last_modified: datetime


@dataclass
class References:
    """Everything the database points at in storage.

    Attributes:
        keys: Every object key a row references.
        charts: Owner and chart id pairs of existing chart rows.
        accounts: Ids of existing accounts.
        chart_count: Chart rows scanned.
        record_count: Leaderboard rows scanned.
        account_count: Account rows scanned.
        ownerless_charts: Chart rows without an owner, whose files can't be addressed.
        newest_write: Latest chart, record or account write, None for an empty database.
    """

    keys: set[str] = field(default_factory=set)
    charts: set[tuple[str, str]] = field(default_factory=set)
    accounts: set[str] = field(default_factory=set)
    chart_count: int = 0
    record_count: int = 0
    account_count: int = 0
    ownerless_charts: int = 0
    newest_write: datetime | None = None


class ScanStorage(Storage):
    """Storage client that also reports object sizes, write times and delete errors.

    Attributes:
        config: Storage settings.
    """

    @staticmethod
    def _stored(item: dict[str, Any]) -> StoredObject:
        """Convert a list_objects_v2 entry.

        Args:
            item: Entry from the Contents list.

        Returns:
            StoredObject: The object.
        """
        return StoredObject(item["Key"], int(item["Size"]), item["LastModified"])

    async def list_top_level(self) -> tuple[list[str], list[StoredObject]]:
        """List the first key segments and the objects stored directly at the root.

        Returns:
            tuple[list[str], list[StoredObject]]: Prefixes ending in a slash, and root objects.
        """
        prefixes: list[str] = []
        objects: list[StoredObject] = []
        paginator = self._client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.config.bucket, Delimiter="/"):
            prefixes.extend(item["Prefix"] for item in page.get("CommonPrefixes", []))
            objects.extend(self._stored(item) for item in page.get("Contents", []))
        return prefixes, objects

    async def list_detailed(self, prefix: str) -> list[StoredObject]:
        """List every object under a prefix with its size and write time.

        Args:
            prefix: Key prefix.

        Returns:
            list[StoredObject]: The objects.
        """
        objects: list[StoredObject] = []
        paginator = self._client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.config.bucket, Prefix=prefix):
            objects.extend(self._stored(item) for item in page.get("Contents", []))
        return objects

    async def delete_batch(self, keys: list[str]) -> list[tuple[str, str]]:
        """Delete up to one batch of keys and report the ones that failed.

        Args:
            keys: Object keys, at most DELETE_BATCH_SIZE.

        Returns:
            list[tuple[str, str]]: Failed keys with their error messages.
        """
        response = await self._client.delete_objects(
            Bucket=self.config.bucket,
            Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
        )
        return [
            (
                error.get("Key", "?"),
                f"{error.get('Code', '')} {error.get('Message', '')}".strip(),
            )
            for error in response.get("Errors", [])
        ]


def progress(message: str, final: bool = False) -> None:
    """Rewrite the current terminal line with a progress message.

    Args:
        message: Text to show.
        final: End the line instead of leaving it for the next update.
    """
    if sys.stdout.isatty():
        sys.stdout.write("\r" + message.ljust(78) + ("\n" if final else ""))
    else:
        sys.stdout.write(message + "\n")
    sys.stdout.flush()


async def scan_database(config: Config) -> References:
    """Collect every storage key the database references.

    Args:
        config: Validated config.yml.

    Returns:
        References: Referenced keys and the charts and accounts that exist.
    """
    database = Database()
    await database.open(small_pool(config.database, 3))
    try:

        async def query(sql: str) -> list[Any]:
            """Run one scan query on its own pooled connection.

            Args:
                sql: Query text.

            Returns:
                list[Any]: The records.
            """
            async with database.connection() as connection:
                return await connection.fetch(sql)

        chart_rows, record_rows, account_rows, newest_rows = await asyncio.gather(
            query(f"SELECT author, id, {', '.join(charts_db.FILE_COLUMNS)} FROM charts"),
            query(
                """
                SELECT c.author, l.chart_id, l.submitter, l.replay_data_hash, l.replay_config_hash
                FROM leaderboards l JOIN charts c ON c.id = l.chart_id
                """
            ),
            query("SELECT sonolus_id, profile_hash, banner_hash FROM accounts"),
            query(
                """
                SELECT GREATEST(
                    (SELECT MAX(GREATEST(created_at, updated_at)) FROM charts),
                    (SELECT MAX(created_at) FROM leaderboards),
                    (SELECT MAX(updated_at) FROM accounts)
                ) AS newest
                """
            ),
        )
    finally:
        await database.close()

    references = References(
        chart_count=len(chart_rows),
        record_count=len(record_rows),
        account_count=len(account_rows),
        newest_write=newest_rows[0]["newest"],
    )
    for row in chart_rows:
        owner, chart_id = row["author"], row["id"]
        if owner is None:
            references.ownerless_charts += 1
            continue
        references.charts.add((owner, chart_id))
        for column in charts_db.FILE_COLUMNS:
            if row[column]:
                references.keys.add(f"{owner}/{chart_id}/{row[column]}")
    for row in record_rows:
        if row["author"] is None or row["submitter"] is None:
            continue
        prefix = f"{row['author']}/{row['chart_id']}/replays/{row['submitter']}/"
        references.keys.add(prefix + row["replay_data_hash"])
        references.keys.add(prefix + row["replay_config_hash"])
    for row in account_rows:
        account_id = row["sonolus_id"]
        references.accounts.add(account_id)
        for kind, column in (("profile", "profile_hash"), ("banner", "banner_hash")):
            if row[column]:
                references.keys.add(f"{account_id}/{kind}/{row[column]}")
                references.keys.add(f"{account_id}/{kind}/{row[column]}_webp")
    return references


async def scan_storage(storage: ScanStorage, concurrency: int) -> list[StoredObject]:
    """List every object in the bucket, one first-level prefix per task.

    Args:
        storage: Open storage client.
        concurrency: Prefixes listed at once.

    Returns:
        list[StoredObject]: Every object.
    """
    print("Listing top-level prefixes...")
    prefixes, objects = await storage.list_top_level()
    print(f"  {len(prefixes)} prefixes, {len(objects)} object(s) at the root")
    semaphore = asyncio.Semaphore(concurrency)
    started = time.monotonic()
    done = 0

    async def scan(prefix: str) -> None:
        """List one prefix and report progress.

        Args:
            prefix: Key prefix.
        """
        nonlocal done
        async with semaphore:
            found = await storage.list_detailed(prefix)
        objects.extend(found)
        done += 1
        if done % PROGRESS_EVERY == 0 or done == len(prefixes):
            progress(
                f"  {done}/{len(prefixes)} prefixes, {len(objects)} objects, "
                f"{time.monotonic() - started:.0f}s",
                final=done == len(prefixes),
            )

    await asyncio.gather(*(scan(prefix) for prefix in prefixes))
    return objects


def classify(key: str, references: References) -> tuple[str, bool]:
    """Work out which kind of file a key is and whether its chart or account still exists.

    Args:
        key: Object key.
        references: Database references.

    Returns:
        tuple[str, bool]: File kind, and whether the owning row exists.
    """
    parts = key.split("/")
    if not all(parts):
        return UNRECOGNIZED, False
    if len(parts) == 3 and parts[1] in ("profile", "banner"):
        return ("avatar" if parts[1] == "profile" else "banner"), parts[0] in references.accounts
    if len(parts) == 3:
        return "chart file", (parts[0], parts[1]) in references.charts
    if len(parts) == 5 and parts[2] == "replays":
        return "replay", (parts[0], parts[1]) in references.charts
    return UNRECOGNIZED, False


def out_of_sync_warnings(
    references: References, objects: list[StoredObject], missing: int
) -> list[str]:
    """Look for signs that the database and the bucket don't belong together or are out of step.

    Args:
        references: Database references.
        objects: Every stored object.
        missing: Number of referenced keys with no object.

    Returns:
        list[str]: One warning per sign found; empty when they look in sync.
    """
    warnings: list[str] = []
    if objects and references.newest_write is not None:
        gap = max(item.last_modified for item in objects) - references.newest_write
        if gap > STALE_LIMIT:
            warnings.append(
                f"the newest object is {gap.days} day(s) newer than the newest database write "
                f"({references.newest_write})"
            )
    if references.keys and missing / len(references.keys) > MISSING_LIMIT:
        warnings.append(
            f"{missing} of {len(references.keys)} referenced keys "
            f"({missing / len(references.keys):.1%}) have no object in the bucket"
        )
    return warnings


def print_references(references: References) -> None:
    """Summarize the database scan.

    Args:
        references: Database references.
    """
    print(
        f"  {references.chart_count} charts, {references.record_count} leaderboard records, "
        f"{references.account_count} accounts -> {len(references.keys)} referenced keys"
    )
    if references.ownerless_charts:
        print(f"  {references.ownerless_charts} chart row(s) have no owner and were ignored")


async def delete_orphans(storage: ScanStorage, keys: list[str]) -> int:
    """Delete keys in batches, showing progress.

    Args:
        storage: Open storage client.
        keys: Keys to delete.

    Returns:
        int: Number of keys that failed to delete.
    """
    failures: list[tuple[str, str]] = []
    for start in range(0, len(keys), DELETE_BATCH_SIZE):
        batch = keys[start : start + DELETE_BATCH_SIZE]
        failures.extend(await storage.delete_batch(batch))
        progress(f"  deleted {min(start + len(batch), len(keys))}/{len(keys)}...")
    progress(f"  deleted {len(keys) - len(failures)}/{len(keys)} objects.", final=True)
    for key, message in failures:
        print(f"  [FAILED] {key}: {message}")
    return len(failures)


async def run(args: argparse.Namespace) -> None:
    """Find unreferenced objects and optionally delete them.

    Args:
        args: Parsed command line.
    """
    config = read_config(args.config)
    print(f"Scanning database {config.database.database}...")
    try:
        references = await scan_database(config)
    except Exception as error:
        raise SystemExit(f"database scan failed, nothing was deleted: {error!r}") from None
    print_references(references)
    if references.chart_count == 0:
        raise SystemExit(
            "the database has no charts; refusing to continue since every file would look "
            "orphaned. Check database.database in the config."
        )

    storage = ScanStorage(config.storage)
    await storage.open()
    try:
        print(f"Scanning bucket {config.storage.bucket}...")
        try:
            objects = await scan_storage(storage, args.concurrency)
        except Exception as error:
            raise SystemExit(f"storage scan failed, nothing was deleted: {error!r}") from None
        total_bytes = sum(item.size for item in objects)
        print(f"  {len(objects)} objects, {format_bytes(total_bytes)}")

        stored_keys = {item.key for item in objects}
        missing = sorted(references.keys - stored_keys)
        if missing:
            print(f"\nMISSING ({len(missing)} referenced keys with no object):")
            for key in missing:
                print(f"  {key}")
        else:
            print("\nNo referenced key is missing from storage.")

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.min_age_minutes)
        orphans: list[StoredObject] = []
        unrecognized: list[StoredObject] = []
        kept_unowned: list[StoredObject] = []
        too_new: list[StoredObject] = []
        categories: dict[str, list[int]] = {}
        for item in sorted(objects, key=lambda stored: stored.key):
            if item.key in references.keys:
                continue
            kind, owner_exists = classify(item.key, references)
            if kind == UNRECOGNIZED and not args.include_unrecognized:
                unrecognized.append(item)
                continue
            if kind != UNRECOGNIZED and not owner_exists and args.skip_unowned:
                kept_unowned.append(item)
                continue
            if item.last_modified > cutoff:
                too_new.append(item)
                continue
            orphans.append(item)
            label = (
                kind
                if kind == UNRECOGNIZED
                else f"{kind}, {'owner exists' if owner_exists else 'owner row gone'}"
            )
            counts = categories.setdefault(label, [0, 0])
            counts[0] += 1
            counts[1] += item.size

        if unrecognized:
            print(
                f"\nUNRECOGNIZED ({len(unrecognized)} keys outside the known layouts, "
                "kept unless --include-unrecognized):"
            )
            for item in unrecognized:
                print(f"  {item.size:>12}  {item.key}")
        if orphans:
            print(f"\nORPHANS ({len(orphans)} objects no row references):")
            for item in orphans:
                print(f"  {item.size:>12}  {item.key}")

        orphan_bytes = sum(item.size for item in orphans)
        print("\nSummary:")
        for label, (count, size) in sorted(categories.items()):
            print(f"  {label:<34} {count:>8} objects  {format_bytes(size):>11}")
        if too_new:
            print(
                f"  skipped {len(too_new)} unreferenced object(s) written in the last "
                f"{args.min_age_minutes} minutes ({format_bytes(sum(i.size for i in too_new))})"
            )
        if kept_unowned:
            print(
                f"  kept {len(kept_unowned)} object(s) of charts/accounts without a row "
                f"(--skip-unowned, {format_bytes(sum(i.size for i in kept_unowned))})"
            )
        if unrecognized:
            print(f"  kept {len(unrecognized)} unrecognized key(s)")
        print(f"  missing referenced keys: {len(missing)}")
        print(
            f"  TOTAL: {len(orphans)} orphaned objects, {orphan_bytes} bytes "
            f"({format_bytes(orphan_bytes)}) of {len(objects)} objects ({format_bytes(total_bytes)})"
        )
        warnings = out_of_sync_warnings(references, objects, len(missing))
        if warnings:
            print("\nWARNING: the database looks out of sync with the bucket:")
            for warning in warnings:
                print(f"  - {warning}")
            print(
                "  It may be a stale copy or another environment's database: files of rows it "
                "doesn't know about show up as orphans."
            )

        if not orphans:
            print("Nothing to delete.")
            return
        if not args.apply:
            print("\nDry run, nothing was deleted. Re-run with --apply to delete them.")
            return
        if warnings and not args.ignore_staleness:
            raise SystemExit(
                "refusing to delete while the database looks out of sync (see the warning above). "
                "Point --config at the live database, or pass --ignore-staleness if you are sure."
            )
        if not args.yes and not confirm(
            f"\nDelete {len(orphans)} objects ({format_bytes(orphan_bytes)}) from bucket "
            f"{config.storage.bucket}? Type 'delete' to continue:",
            "delete",
        ):
            print("Aborted.")
            return

        print("Re-checking the database before deleting...")
        try:
            fresh = await scan_database(config)
        except Exception as error:
            raise SystemExit(f"database re-check failed, nothing was deleted: {error!r}") from None
        if fresh.chart_count == 0:
            raise SystemExit("the database re-check found no charts, nothing was deleted")
        keys = [item.key for item in orphans if item.key not in fresh.keys]
        if len(keys) != len(orphans):
            print(f"  {len(orphans) - len(keys)} key(s) became referenced meanwhile and are kept")
        failed = await delete_orphans(storage, keys)
        if failed:
            raise SystemExit(f"{failed} object(s) could not be deleted")
    finally:
        await storage.close()


def main() -> None:
    """Parse the command line and look for orphaned storage objects."""
    parser = argparse.ArgumentParser(
        description=(
            "Find storage objects no database row references (chart files, replays, avatars, "
            "banners) and optionally delete them. Dry run by default."
        )
    )
    add_config_argument(parser)
    parser.add_argument("--apply", action="store_true", help="delete the orphaned objects")
    parser.add_argument(
        "--yes", action="store_true", help="with --apply, skip the confirmation prompt"
    )
    parser.add_argument(
        "--min-age-minutes",
        type=int,
        default=60,
        help="leave unreferenced objects younger than this alone, since uploads write files "
        "before their row (default: 60)",
    )
    parser.add_argument(
        "--skip-unowned",
        action="store_true",
        help="keep files of charts and accounts that no longer have a row, like the old find_orphans",
    )
    parser.add_argument(
        "--include-unrecognized",
        action="store_true",
        help="also treat keys outside the known layouts as orphans",
    )
    parser.add_argument(
        "--ignore-staleness",
        action="store_true",
        help="delete even when the database looks out of sync: the newest object is over a day "
        "newer than the newest database write, or over 1%% of referenced keys have no object",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=LIST_CONCURRENCY,
        help=f"prefixes listed at once (default: {LIST_CONCURRENCY})",
    )
    args = parser.parse_args()
    if args.min_age_minutes < 0:
        parser.error("--min-age-minutes must not be negative")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
