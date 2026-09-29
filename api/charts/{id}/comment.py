import math

from fastapi import APIRouter, Request, HTTPException, status, Query
from typing import Optional

from core import ChartFastAPI

from database import accounts, comments, staff_actions
from helpers.session import get_session, Session
from helpers.chart_access import ensure_chart_visible

from helpers.models import CommentRequest

router = APIRouter()


@router.post("/")
async def main(
    request: Request,
    id: str,
    data: CommentRequest,
    session: Session = get_session(
        enforce_auth=True, enforce_type="game", allow_banned_users=False
    ),
):
    # exposed to public
    # authentication needed

    app: ChartFastAPI = request.app

    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )
    if len(data.content) > 1500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Comments cannot be larger than 1500 characters.",
        )
    await ensure_chart_visible(app, session, id)
    user = await session.user()
    query = comments.create_comment(
        user.sonolus_id, user.sonolus_username, id, data.content
    )
    async with app.db_acquire() as conn:
        result = await conn.fetchrow(query)
        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
            )
    return {"result": "success"}


@router.delete("/{comment_id}/")
async def main(
    request: Request,
    id: str,
    comment_id: int,
    session: Session = get_session(
        enforce_auth=True, enforce_type="game", allow_banned_users=False
    ),
):
    # exposed to public
    # authentication needed

    app: ChartFastAPI = request.app

    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )
    user = await session.user()
    if user.mod:
        query = comments.delete_comment(comment_id)
    else:
        query = comments.delete_comment(comment_id, user.sonolus_id)
    async with app.db_acquire() as conn:
        result = await conn.fetchrow(query)
        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chart or comment not found.",
            )
        if user.mod and user.sonolus_id != result.commenter:
            await conn.execute(
                staff_actions.log_action(
                    actor_id=user.sonolus_id,
                    action="comment_delete",
                    target_type="comment",
                    target_id=str(comment_id),
                    previous_value=result.content,
                )
            )
    d = result.model_dump()
    if user.mod:
        d["mod"] = True
    if user.sonolus_id == d["commenter"]:
        d["owner"] = True
    return d


@router.get("/")
async def main(
    request: Request,
    id: str,
    page: Optional[int] = Query(0, ge=0),
    session: Session = get_session(
        enforce_auth=False, enforce_type="game", allow_banned_users=False
    ),
):
    app: ChartFastAPI = request.app

    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )
    await ensure_chart_visible(app, session, id)

    user = None
    if session.auth:
        user = await session.user()

    async with app.db_acquire() as conn:
        comment_query, count_query = comments.get_comments(
            id, sonolus_id=user.sonolus_id if user else None, page=page
        )

        count_result = await conn.fetchrow(count_query)
        total_count = count_result.total_count if count_result else 0
        page_count = math.ceil(total_count / 10) if total_count > 0 else 0

        if page_count == 0 or page >= page_count:
            return {"data": [], "pageCount": page_count}

        result = await conn.fetch(comment_query)
        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
            )

        account_dict = {
            account.sonolus_id: account
            for account in await conn.fetch(
                accounts.get_public_account_batch(
                    list(set([comment.commenter for comment in result]))
                )
            )
        }

    data = [
        {
            **row.model_dump(),
            "created_at": int(row.created_at.timestamp() * 1000),
            "deleted_at": (
                int(row.deleted_at.timestamp() * 1000) if row.deleted_at else None
            ),
            "account": account_dict.get(row.commenter),
        }
        for row in result
    ]
    for comment in data:
        if comment["deleted_at"]:
            comment["content"] = (
                f"[DELETED]\nMod View:\n{'-'*10}\n{comment['content']}"
                if (user and user.mod)
                else "[DELETED]"
            )
    ret = {"data": data, "pageCount": page_count}
    if user and user.mod:
        ret["mod"] = True
        if user.admin:
            ret["admin"] = True
    return ret
