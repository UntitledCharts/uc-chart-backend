from fastapi import APIRouter, Request, HTTPException, status

from core import ChartFastAPI
from database import promotions, accounts
from helpers.models import PromotionClickData
from helpers.session import get_session, Session

router = APIRouter()


@router.post("/")
async def main(
    request: Request,
    data: PromotionClickData,
    session: Session = get_session(scopes=[]),
):
    app: ChartFastAPI = request.app

    if len(data.chart_id) != 32 or not data.chart_id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )

    async with app.db_acquire() as conn:
        result = await conn.fetchrow(
            promotions.resolve_click(data.chart_id, data.view_code)
        )
        counted = bool(result and result.total_count > 0)
        # guests still convert the view to a click, just without user attribution
        if counted and session.auth and not session.is_oauth:
            user = await session.user()
            if user:
                await conn.execute(accounts.record_ad_click(user.sonolus_id))

    return {"counted": counted}
