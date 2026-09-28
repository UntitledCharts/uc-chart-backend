from core import ChartFastAPI

from fastapi import APIRouter, Request, HTTPException, status

from helpers.session import get_session, Session
from helpers.models import Account

from database import accounts, staff_actions

from typing import Optional

router = APIRouter()


async def get_actor(
    request: Request, session: Session, target_id: str
) -> Optional[Account]:
    """
    Server secret, or an admin session. Returns the actor, or None for the secret
    (which has no account to attribute the action to).
    """
    app: ChartFastAPI = request.app

    async with app.db_acquire() as conn:
        target = await conn.fetchrow(accounts.get_public_account(target_id))

    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found."
        )

    if request.headers.get(app.auth_header) == app.auth:
        return None

    actor = await session.user()
    if not actor or not actor.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not admin")

    if actor.sonolus_id == target.sonolus_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You can't moderate yourself.",
        )
    if target.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can't moderate an admin."
        )

    return actor


@router.patch("/ban/")
async def ban_user(
    request: Request,
    id: str,
    delete: bool = True,
    session: Session = get_session(allow_banned_users=False),
):
    app: ChartFastAPI = request.app

    actor = await get_actor(request, session, id)

    async with app.db_acquire() as conn:
        await conn.execute(accounts.set_banned(id, True))
        # marks the whole account (not its charts) so an unban restores everything
        await conn.execute(accounts.set_account_deleted(id, delete))
        if actor:
            await conn.execute(
                staff_actions.log_action(
                    actor_id=actor.sonolus_id,
                    action="ban",
                    target_type="account",
                    target_id=id,
                    previous_value="False",
                    new_value="True",
                )
            )

    return {"result": "success"}


@router.patch("/unban/")
async def unban_user(
    request: Request,
    id: str,
    session: Session = get_session(allow_banned_users=False),
):
    app: ChartFastAPI = request.app

    actor = await get_actor(request, session, id)

    async with app.db_acquire() as conn:
        await conn.execute(accounts.set_banned(id, False))
        await conn.execute(accounts.set_account_deleted(id, False))
        if actor:
            await conn.execute(
                staff_actions.log_action(
                    actor_id=actor.sonolus_id,
                    action="ban",
                    target_type="account",
                    target_id=id,
                    previous_value="True",
                    new_value="False",
                )
            )

    return {"result": "success"}
