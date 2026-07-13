import asyncio
from fastapi import APIRouter, Request, HTTPException, status

from database import charts
from helpers.session import get_session, Session

from core import ChartFastAPI

router = APIRouter()


@router.delete("/")
async def main(
    request: Request,
    id: str,
    session: Session = get_session(
        enforce_auth=True,
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
    user = await session.user()
    # oauth tokens never get admin powers, only the user's own charts
    admin = user.admin and not session.is_oauth
    if admin:
        query = charts.delete_chart(id, confirm_change=True)
    else:
        query = charts.delete_chart(id, session.sonolus_id, confirm_change=True)
    async with app.db_acquire() as conn:
        exists = await conn.fetchrow(query)
    if exists:
        async with app.s3_session_getter() as s3:
            bucket = await s3.Bucket(app.s3_bucket)
            tasks = []
            prefix = f"{session.sonolus_id}/{id}/"
            objects = [obj async for obj in bucket.objects.filter(Prefix=prefix)]
            if objects:
                tasks = [obj.delete() for obj in objects]
                await asyncio.gather(*tasks)
    else:
        if admin:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chart not found for any user!",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
        )
    d = exists.model_dump()
    if admin:
        d["admin"] = True
    if user.sonolus_id == d["author"]:
        d["owner"] = True
    return d
