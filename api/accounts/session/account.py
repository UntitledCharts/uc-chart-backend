import datetime
from core import ChartFastAPI

from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import JSONResponse

from database import accounts

from helpers.session import get_session, Session

router = APIRouter()


@router.get("/")
async def main(
    request: Request,
    session: Session = get_session(enforce_auth=True, scopes=["user:read"]),
):
    app: ChartFastAPI = request.app

    return_keys = [
        "sonolus_id",
        "sonolus_handle",
        "sonolus_username",
        "created_at",
        "mod",
        "admin",
        "banned",
    ]
    return_val = {}
    for key, value in (await session.user()).model_dump().items():
        if key in return_keys:
            return_val[key] = value

    async with app.db_acquire() as conn:
        result = await conn.fetchrow(
            accounts.get_unread_notifications_count(session.sonolus_id)
        )
        return_val["unread_notifications"] = result.total_count if result else 0

    return return_val
