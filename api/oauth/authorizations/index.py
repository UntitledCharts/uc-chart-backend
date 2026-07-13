from fastapi import APIRouter, Request, HTTPException, status

from database import oauth

from helpers.oauth import SCOPE_DESCRIPTIONS
from helpers.session import get_session, Session

from core import ChartFastAPI

router = APIRouter()


@router.get("/")
async def main(
    request: Request,
    session: Session = get_session(enforce_auth=True, enforce_type="external"),
):
    """Every app the user has granted access to, for the authorized apps page."""
    app: ChartFastAPI = request.app

    async with app.db_acquire() as conn:
        authorizations = await conn.fetch(oauth.get_authorizations(session.sonolus_id))

    return [
        {
            "client_id": authorization.client_id,
            "name": authorization.name,
            "description": authorization.description,
            "scopes": [
                {"scope": scope, "description": SCOPE_DESCRIPTIONS[scope]}
                for scope in authorization.scopes
            ],
            "authorized_at": authorization.authorized_at,
            "last_used_at": authorization.last_used_at,
        }
        for authorization in authorizations
    ]


@router.delete("/{client_id}/")
async def revoke(
    request: Request,
    client_id: str,
    session: Session = get_session(enforce_auth=True, enforce_type="external"),
):
    app: ChartFastAPI = request.app

    async with app.db_acquire() as conn:
        revoked = await conn.fetchrow(
            oauth.revoke_authorization(session.sonolus_id, client_id)
        )

    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You haven't authorized this app.",
        )

    return {"result": "success"}
