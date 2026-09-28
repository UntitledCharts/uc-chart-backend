import asyncpg
from fastapi import APIRouter, Request

from core import ChartFastAPI
from database import promotions, charts, accounts
from helpers.session import get_session, Session
from helpers.promotions import choose_promotion, generate_view_code

router = APIRouter()

_MAX_CODE_ATTEMPTS = 6


@router.get("/")
async def main(
    request: Request,
    session: Session = get_session(scopes=[]),
):
    app: ChartFastAPI = request.app

    async with app.db_acquire() as conn:
        active = await conn.fetch(promotions.get_active_promotions())
    if not active:
        return {"promotion": None}

    # only a real Sonolus login is profiled; oauth tokens and guests are not
    user = None
    if session.auth and not session.is_oauth:
        user = await session.user()
    is_logged_in = bool(user)

    chosen = choose_promotion(user, is_logged_in, active)
    if not chosen:
        return {"promotion": None}

    view_code = None
    async with app.db_acquire() as conn:
        for _ in range(_MAX_CODE_ATTEMPTS):
            code = generate_view_code()
            try:
                await conn.execute(promotions.insert_view(chosen.id, code))
                view_code = code
                break
            except asyncpg.exceptions.UniqueViolationError:
                continue
        if view_code is None:
            return {"promotion": None}
        await conn.execute(promotions.increment_view_count(chosen.id))
        if is_logged_in:
            await conn.execute(accounts.record_ad_view(user.sonolus_id))

        chart = await conn.fetchrow(
            charts.get_chart_by_id(
                chosen.chart_id, sonolus_id=user.sonolus_id if user else None
            )
        )
    if not chart:
        return {"promotion": None}

    return {
        "promotion": {"chart_id": chosen.chart_id, "view_code": view_code},
        "data": chart.model_dump(),
        "asset_base_url": app.s3_asset_base_url,
    }
