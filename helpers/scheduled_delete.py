import asyncio
import traceback

from core import ChartFastAPI
from database import charts, accounts, promotions
from helpers.delete import delete_chart_files_from_s3, delete_from_s3

DELETION_GRACE_DAYS: int = 14
PROMOTION_VIEW_TTL_HOURS: int = 24
CHECK_INTERVAL_SECONDS: int = 120
CHART_PURGE_BATCH: int = 500
ACCOUNT_PURGE_BATCH: int = 100
# database-wide mutex so only one worker process purges per cycle
_ADVISORY_LOCK_KEY: int = 6579564


async def _purge_charts(app: ChartFastAPI) -> None:
    async with app.db_acquire() as conn:
        purged = await conn.fetch(
            charts.purge_expired_charts(DELETION_GRACE_DAYS, CHART_PURGE_BATCH)
        )
    for item in purged:
        try:
            await delete_chart_files_from_s3(app, item.author, item.id)
        except Exception:
            traceback.print_exc()


async def _purge_accounts(app: ChartFastAPI) -> None:
    async with app.db_acquire() as conn:
        pending = await conn.fetch(
            accounts.get_accounts_pending_purge(DELETION_GRACE_DAYS, ACCOUNT_PURGE_BATCH)
        )
    for item in pending:
        try:
            # S3 first: delete_from_s3 needs the account's leaderboard rows, which
            # the account delete cascades away.
            await delete_from_s3(app, item.sonolus_id)
            async with app.db_acquire() as conn:
                await conn.execute(
                    accounts.purge_delete_account(item.sonolus_id, DELETION_GRACE_DAYS)
                )
        except Exception:
            traceback.print_exc()


async def _expire_promotion_views(app: ChartFastAPI) -> None:
    async with app.db_acquire() as conn:
        await conn.execute(promotions.expire_views(PROMOTION_VIEW_TTL_HOURS))
        await conn.execute(promotions.delete_inactive_user_views())


async def run_deletion_cycle(app: ChartFastAPI) -> None:
    async with app.db.acquire() as lock_conn:
        acquired = await lock_conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", _ADVISORY_LOCK_KEY
        )
        if not acquired:
            return
        try:
            await _purge_charts(app)
            await _purge_accounts(app)
            await _expire_promotion_views(app)
        finally:
            await lock_conn.fetchval("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK_KEY)


async def deletion_loop(app: ChartFastAPI) -> None:
    while True:
        try:
            await run_deletion_cycle(app)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def start_deletion_worker(app: ChartFastAPI) -> None:
    app.deletion_task = asyncio.create_task(deletion_loop(app))
