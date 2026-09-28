from fastapi import APIRouter, Request, HTTPException, status, Query

from database import charts, staff_actions
from helpers.session import get_session, Session
from helpers.delete import delete_chart_files_from_s3

from core import ChartFastAPI

router = APIRouter()


async def _hard_delete(app: ChartFastAPI, chart_id: str):
    async with app.db_acquire() as conn:
        deleted = await conn.fetchrow(charts.delete_chart(chart_id, confirm_change=True))
    if deleted:
        await delete_chart_files_from_s3(app, deleted.author, chart_id)
    return deleted


@router.delete("/")
async def main(
    request: Request,
    id: str,
    instant: bool = Query(False),
    session: Session = get_session(
        enforce_auth=False,
        enforce_type=False,
        allow_banned_users=False,
        scopes=["chart:delete"],
    ),
):
    app: ChartFastAPI = request.app

    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )

    # only the internal token (scripts) may skip the reversible deletion window
    if request.headers.get(app.auth_header) == app.auth:
        if instant:
            deleted = await _hard_delete(app, id)
            if not deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
                )
            return {"result": "success", "instant": True}
        async with app.db_acquire() as conn:
            result = await conn.fetchrow(charts.soft_delete_chart(id))
        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
            )
        return {"result": "success", "instant": False}

    user = await session.user()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in."
        )

    # oauth tokens never get mod/admin powers, only the user's own charts
    elevated = (user.mod or user.admin) and not session.is_oauth

    if elevated:
        query = charts.soft_delete_chart(id)
    else:
        query = charts.soft_delete_chart(id, user.sonolus_id)

    async with app.db_acquire() as conn:
        result = await conn.fetchrow(query)
        if not result:
            if elevated:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Chart not found for any user!",
                )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
            )
        if elevated and user.sonolus_id != result.author:
            await conn.execute(
                staff_actions.log_action(
                    actor_id=user.sonolus_id,
                    action="delete",
                    target_type="chart",
                    target_id=id,
                    previous_value=None,
                    new_value="soft_deleted",
                )
            )

    d = result.model_dump()
    if elevated:
        if user.admin:
            d["admin"] = True
        if user.mod:
            d["mod"] = True
    if user.sonolus_id == d["author"]:
        d["owner"] = True
    return d
