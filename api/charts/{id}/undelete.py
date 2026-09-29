from fastapi import APIRouter, Request, HTTPException, status

from database import charts, staff_actions
from helpers.session import get_session, Session

from core import ChartFastAPI

router = APIRouter()


@router.post("/")
async def main(
    request: Request,
    id: str,
    session: Session = get_session(
        enforce_auth=False,
        enforce_type=False,
        allow_banned_users=False,
    ),
):
    app: ChartFastAPI = request.app

    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )

    user = None
    if request.headers.get(app.auth_header) != app.auth:
        user = await session.user()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in."
            )
        if not (user.mod or user.admin):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="You are not a moderator!"
            )

    async with app.db_acquire() as conn:
        result = await conn.fetchrow(charts.undelete_chart(id))
        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chart not found or not pending deletion.",
            )
        if user:
            await conn.execute(
                staff_actions.log_action(
                    actor_id=user.sonolus_id,
                    action="undelete",
                    target_type="chart",
                    target_id=id,
                    previous_value="soft_deleted",
                    new_value=None,
                )
            )

    d = result.model_dump()
    if user:
        if user.admin:
            d["admin"] = True
        if user.mod:
            d["mod"] = True
        if user.sonolus_id == d["author"]:
            d["owner"] = True
    return d
