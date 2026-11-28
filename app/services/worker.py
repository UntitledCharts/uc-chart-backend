import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from app.context import AppContext
from app.database import accounts as accounts_db
from app.database import charts as charts_db
from app.database import login_requests as login_requests_db
from app.database import oauth as oauth_db
from app.database import promotions as promotions_db
from app.database import sessions as sessions_db
from app.models import present
from app.services import og_cache, purge

logger = logging.getLogger("uc.backend.worker")

ADVISORY_LOCK_KEY = 0x5543_5752
PURGE_BATCH = 100

_last_og_sweep = 0.0


async def publish_scheduled(context: AppContext) -> None:
    """Publish charts whose scheduled publish time has passed.

    Args:
        context: App context.
    """
    batch = context.config.worker.cleanup_batch_size
    async with context.database.transaction() as connection:
        published = await charts_db.publish_due_charts(
            connection, batch, context.constants.charts.min_public_chart_seconds
        )
    if not published:
        return
    async with context.database.connection() as connection:
        rows = await charts_db.get_charts(connection, [chart_id for chart_id, _ in published])
    for chart_id, first in published:
        row = rows.get(chart_id)
        if row is None:
            continue
        chart_summary = present.discord_chart(row, context.storage)
        context.discord.visibility_changed(
            chart_summary, "scheduled", "PUBLIC", "scheduled publish"
        )
        if first:
            context.discord.first_publish(chart_summary)


async def purge_expired(context: AppContext) -> None:
    """Purge charts and accounts whose deletion grace period is over.

    Args:
        context: App context.
    """
    grace_days = context.constants.charts.deletion_grace_days
    async with context.database.connection() as connection:
        charts = await charts_db.list_charts_to_purge(connection, grace_days, PURGE_BATCH)
        accounts = await accounts_db.list_accounts_to_purge(connection, grace_days, PURGE_BATCH)
    for chart_id in charts:
        try:
            await purge.purge_chart(context, chart_id, grace_days)
        except Exception:
            logger.exception("failed to purge chart %s", chart_id)
    for sonolus_id in accounts:
        try:
            await purge.purge_account(context, sonolus_id, grace_days)
        except Exception:
            logger.exception("failed to purge account %s", sonolus_id)


async def clean_up(context: AppContext) -> None:
    """Delete one batch of expired sessions, logins, OAuth data and promotion rows.

    Args:
        context: App context.
    """
    batch = context.config.worker.cleanup_batch_size
    window = context.constants.promotions.view_window_hours
    async with context.database.connection() as connection:
        await sessions_db.delete_expired_sessions(connection, batch)
        await login_requests_db.delete_expired_login_requests(connection, batch)
        await oauth_db.delete_expired_codes(connection, batch)
        await oauth_db.delete_expired_tokens(connection, batch)
        await promotions_db.expire_stale_rows(connection, window, batch)


async def delete_stale_og_images(context: AppContext) -> None:
    """Delete stored link preview images older than their maximum age, at most once per interval.

    Args:
        context: App context.
    """
    global _last_og_sweep
    constants = context.constants.og_images
    now = time.monotonic()
    if _last_og_sweep and now - _last_og_sweep < constants.cleanup_interval_seconds:
        return
    _last_og_sweep = now
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=constants.max_age_seconds)
    stale = await context.storage.list_keys_older_than(og_cache.STORAGE_PREFIX, cutoff)
    if stale:
        await context.storage.discard_keys(stale)


async def run_cycle(context: AppContext) -> None:
    """Run one worker cycle if no other worker holds the lock.

    Args:
        context: App context.
    """
    async with context.database.connection() as lock_connection:
        if not await lock_connection.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY):
            return
        try:
            for step in (
                publish_scheduled,
                purge_expired,
                clean_up,
                delete_stale_og_images,
            ):
                try:
                    await step(context)
                except Exception:
                    logger.exception("worker step %s failed", step.__name__)
        finally:
            await lock_connection.fetchval("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)


async def worker_loop(context: AppContext) -> None:
    """Run worker cycles forever.

    Args:
        context: App context.
    """
    while True:
        try:
            await run_cycle(context)
        except Exception:
            logger.exception("worker cycle failed")
        await asyncio.sleep(context.config.worker.interval_seconds)
