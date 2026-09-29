from fastapi import APIRouter, Request, HTTPException, status
from core import ChartFastAPI

from database import charts
from helpers.session import get_session, Session
from helpers.chart_access import ensure_chart_visible

from helpers.models import Like

router = APIRouter()


@router.post("/")
async def main(
    request: Request,
    id: str,
    data: Like,
    session: Session = get_session(enforce_auth=True, allow_banned_users=False),
):
    # exposed to public
    # authentication needed

    app: ChartFastAPI = request.app

    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )
    await ensure_chart_visible(app, session, id)
    if data.type == "like":
        query = charts.add_like(id, session.sonolus_id)
    elif data.type == "unlike":
        query = charts.remove_like(id, session.sonolus_id)
    async with app.db_acquire() as conn:
        await conn.execute(query)
    return {"result": "success possibly"}
