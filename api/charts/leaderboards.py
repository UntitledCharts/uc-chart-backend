"""
Unlike charts/{id}/leaderboards, returns all public records
"""

import asyncio
import math
from typing import Literal
from fastapi import APIRouter, Query, Request

from core import ChartFastAPI
from database import accounts, charts, leaderboards

router = APIRouter()


async def get_records(
    random: bool, limit: int, app: ChartFastAPI, page: int | None = None
):
    async with app.db_acquire() as conn:
        if random:
            records = await conn.fetch(
                leaderboards.get_random_leaderboard_records(limit)
            )
        else:
            leaderboard_query, count_query = leaderboards.get_public_records(
                limit, page
            )
            records = await conn.fetch(leaderboard_query)

    if not records:
        return {"data": [], "pageCount": 0} if not random else {"data": []}

    chart_ids = list(set([record.chart_id for record in records]))
    submitter_ids = list(set([record.submitter for record in records]))

    async def _fetch_charts():
        async with app.db_acquire() as c:
            return await c.fetch(charts.get_chart_by_id_batch(chart_ids))

    async def _fetch_accounts():
        async with app.db_acquire() as c:
            return await c.fetch(accounts.get_public_account_batch(submitter_ids))

    async def _fetch_count():
        if random or limit == 3:
            return None
        async with app.db_acquire() as c:
            return await c.fetchrow(count_query)

    chart_list, account_list, count_result = await asyncio.gather(
        _fetch_charts(), _fetch_accounts(), _fetch_count()
    )
    chart_dict = {chart.id: chart for chart in chart_list}
    account_dict = {account.sonolus_id: account for account in account_list}

    response = {"data": []}
    for record in records:
        response["data"].append(
            {
                "data": record.model_dump(),
                "chart": chart_dict[record.chart_id],
                "submitter": account_dict.get(record.submitter),
                "asset_base_url": app.s3_asset_base_url,
            }
        )

    if count_result:
        response["pageCount"] = math.ceil(count_result.total_count / 10)

    return response


@router.get("/random/")
async def get(request: Request, limit: int = Query(10, gt=0, le=10)):
    app: ChartFastAPI = request.app

    return await get_records(random=True, limit=limit, app=app)


@router.get("/")
async def get(request: Request, limit: int = Query(10, gt=0, le=10), page: int = 0):
    app: ChartFastAPI = request.app

    return await get_records(random=False, limit=limit, app=app, page=page)
