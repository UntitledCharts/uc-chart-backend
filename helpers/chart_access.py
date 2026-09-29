from fastapi import HTTPException, status

from core import ChartFastAPI
from database import charts
from helpers.session import Session


async def ensure_chart_visible(
    app: ChartFastAPI, session: Session, chart_id: str
) -> None:
    async with app.db_acquire() as conn:
        chart = await conn.fetchrow(charts.get_chart_by_id(chart_id))

    if not chart:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
        )

    if chart.deleted_at is None and chart.account_deleted_at is None:
        return

    user = await session.user() if session.auth else None
    if not (user and user.mod and not session.is_oauth):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
        )
