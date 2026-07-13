from fastapi import APIRouter, Request, HTTPException, Query, status
from core import ChartFastAPI

from database import charts
from helpers.session import get_session, Session

router = APIRouter()


@router.get("/")
async def main(
    request: Request,
    id: str,
    is_preview: bool = Query(False),
    # public charts need no permission, private ones need chart:read
    session: Session = get_session(scopes=[]),
):
    app: ChartFastAPI = request.app

    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )

    query = charts.get_chart_by_id(id, sonolus_id=session.sonolus_id)

    async with app.db_acquire() as conn:
        result = await conn.fetchrow(query)

        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
            )

        user = None
        if session.auth:
            user = await session.user()

        # oauth tokens never get mod powers, they see what their user sees
        if user and user.mod and not session.is_oauth:
            res = {
                "data": result.model_dump(),
                "asset_base_url": app.s3_asset_base_url,
                "mod": True,
                "owner": result.author == session.sonolus_id,
            }
            if user.admin:
                res["admin"] = True
            return res

        is_owner = result.author == session.sonolus_id

        if result.status == "PRIVATE":
            if not is_owner:
                # 404 before the scope check, so a missing scope can't confirm it exists
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
                )
            session.require_scopes("chart:read")

    return {
        "data": result.model_dump(),
        "asset_base_url": app.s3_asset_base_url,
        "owner": result.author == session.sonolus_id,
    }
